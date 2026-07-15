"""Command-line interface for atlas."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import unquote, urlparse
from uuid import uuid4

import typer
from pydantic import ValidationError
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from atlas import __version__
from atlas.adapters import DirectFileAdapter, SiteMirrorAdapter
from atlas.adaptive import (
    AdaptiveControls,
    AdaptiveScheduler,
    build_plan_for_items,
    classify_file_size,
    default_adaptive_controls,
    plan_items_from_site_scan,
    scan_direct_file,
    scan_site,
)
from atlas.aria2_rpc import (
    Aria2RpcQueuedDownload,
    Aria2RpcSession,
    Aria2RpcStartupError,
)
from atlas.backends import (
    FileDownloadEngine,
    SiteMirrorEngine,
    can_attempt_verified_curl_fallback,
    filename_from_url,
    is_tls_certificate_failure,
)
from atlas.batch import (
    BatchControl,
    BatchItemContext,
    BatchOperatorController,
    load_batch_file,
    run_batch_adaptive,
    run_batch_concurrent,
)
from atlas.config import AtlasSettings, load_config, settings_as_toml
from atlas.doctor import run_doctor
from atlas.errors import AtlasError, ConfigError, EngineError
from atlas.formats import filter_formats, format_bytes, format_duration, sort_formats
from atlas.hub import EngineRouter
from atlas.logging import configure_logging
from atlas.media_capabilities import MediaCapabilityCatalog, MediaCapabilityResolver
from atlas.menu import (
    MenuDownloadOptions,
    MenuUnavailable,
    can_auto_launch_menu,
    has_interactive_tty,
    run_interactive_menu,
)
from atlas.models import (
    AdaptiveDownloadPlan,
    AdaptivePoliteness,
    Aria2UriSelector,
    AudioCodec,
    AudioDownloadOptions,
    BatchEntry,
    BatchItemResult,
    BatchKind,
    BatchSummary,
    CertificateType,
    Container,
    DirectoryMirrorOptions,
    DoctorReport,
    DownloadAttrMode,
    DownloadEngineChoice,
    DownloadPlan,
    DownloadResult,
    DownloadStatus,
    EngineKind,
    EngineRoute,
    FileBackendChoice,
    FileDownloadOptions,
    FileSizeClass,
    FormatInfo,
    FormatSort,
    FpsChoice,
    HdrChoice,
    HttpsEnforceMode,
    HubKind,
    HubRequest,
    InfoOptions,
    MediaInfo,
    MetalinkPreferredProtocol,
    OrganizeMode,
    PreferFamily,
    ProgressEvent,
    ProgressMode,
    ProgressPhase,
    QualityIntent,
    ResolutionChoice,
    SiteBackendChoice,
    SiteDownloadOptions,
    SubtitleMode,
    VerifySigMode,
    VideoCodecChoice,
    VideoDownloadOptions,
    WorkBucket,
    WorkItem,
)
from atlas.optimizer import DownloadOptimizer, HubExecutionPlan, plan_as_dict
from atlas.passthrough import (
    BackendCommandPlan,
    BackendTool,
    plan_backend_command,
    run_backend_command,
)
from atlas.passthrough import (
    plan_as_dict as backend_plan_as_dict,
)
from atlas.paths import config_path, log_dir, safe_filename
from atlas.planner import SmartPlanner
from atlas.preflight import ensure_download_dependencies
from atlas.presets import DEFAULT_AUDIO_FORMAT, DEFAULT_VIDEO_FORMAT
from atlas.private_files import ensure_private_directory, write_private_text
from atlas.progress import (
    BatchProgressReporter,
    FileProgressReporter,
    ProgressHook,
    RichProgressReporter,
    WorkPanelContext,
    create_batch_postprocessor_hook,
    create_batch_progress_hook,
    create_postprocessor_hook,
    create_progress_hook,
    resolve_progress_mode,
    should_use_alternate_screen,
)
from atlas.runner import ProcessControl
from atlas.sessions import batch_session, site_session
from atlas.setup import (
    SetupMode,
    SetupPlan,
    SetupResult,
    UpdatePlan,
    apply_setup_plan,
    build_setup_plan,
    build_update_plan,
    run_update_plan,
)
from atlas.theme import (
    ATLAS_ACTIVE_STYLE,
    ATLAS_ERROR_STYLE,
    ATLAS_MUTED_STYLE,
    ATLAS_PANEL_STYLE,
    ATLAS_PATH_STYLE,
    ATLAS_SUCCESS_STYLE,
    ATLAS_TITLE_STYLE,
    ATLAS_WARNING_STYLE,
    AtlasThemeName,
    configure_visuals,
    status_glyph,
    table_box,
    themed_console,
    visual_join,
    visual_options,
)
from atlas.urls import is_explicit_playlist_url
from atlas.views import ActiveWorkRow, FailureRow, ProgressMetric, SmartSessionView, ViewField

_TEMP_STREAM_RE = re.compile(r"\.f\d+(?=\.)")

if TYPE_CHECKING:
    from atlas.engine import MediaProbe, YtdlpEngine

console = themed_console()


class BatchRetryMode:
    """Internal retry selectors for saved batch sessions."""

    failed = "failed"
    checksum = "checksum"
    skipped_unknown = "skipped_unknown"
    canceled = "canceled"
    resume = "resume"


class SessionPreviewChoice(StrEnum):
    """Saved-session preview panes for operator inspection."""

    none = "none"
    plan = "plan"
    backend = "backend"
    manifest = "manifest"
    summary = "summary"
    retry = "retry"
    failed = "failed"
    errors = "errors"
    logs = "logs"
    config = "config"


class SessionCommandChoice(StrEnum):
    """Copyable saved-session operator command choices."""

    none = "none"
    retry = "retry"
    resume = "resume"
    export_failed = "export-failed"
    inspect = "inspect"
    backend = "backend"


class SessionPanelChoice(StrEnum):
    """Saved-session operator panels."""

    overview = "overview"
    queue = "queue"
    active = "active"
    completed = "completed"
    canceled = "canceled"
    failed = "failed"
    scheduler = "scheduler"
    logs = "logs"
    summary = "summary"


class SessionStatusFilter(StrEnum):
    """Saved-session item status filters."""

    all = "all"
    success = "success"
    failed = "failed"
    skipped = "skipped"
    canceled = "canceled"
    dry_run = "dry-run"


app = typer.Typer(
    name="atlas",
    help="atlas: intent-first downloads for media, files, batches, and mirrors.",
    invoke_without_command=True,
    rich_markup_mode=None,
)
config_app = typer.Typer(
    help="Show atlas configuration.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(config_app, name="config")


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed Atlas version and exit.",
            is_eager=True,
        ),
    ] = False,
    no_menu: Annotated[
        bool,
        typer.Option("--no-menu", help="Show help instead of opening the no-arg menu."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Suppress the no-arg menu for automation.", hidden=True),
    ] = False,
    theme: Annotated[
        AtlasThemeName,
        typer.Option("--theme", help="Terminal color theme."),
    ] = AtlasThemeName.auto,
    plain: Annotated[
        bool,
        typer.Option("--plain", help="Disable color and Unicode for simple terminals."),
    ] = False,
    no_unicode: Annotated[
        bool,
        typer.Option("--no-unicode", help="Use ASCII boxes, icons, and progress bars."),
    ] = False,
    no_animation: Annotated[
        bool,
        typer.Option("--no-animation", help="Disable animated progress shimmer and pulses."),
    ] = False,
) -> None:
    """Launch atlas or dispatch a subcommand."""

    if version:
        typer.echo(f"atlas {__version__}")
        raise typer.Exit()
    _configure_cli_visuals(
        theme=theme,
        plain=plain,
        no_unicode=no_unicode,
        no_animation=no_animation,
    )
    if ctx.invoked_subcommand is not None:
        return
    if _should_auto_launch_menu(no_menu=no_menu, json_output=json_output):
        _launch_menu()
        raise typer.Exit()
    console.print(ctx.get_help())
    raise typer.Exit()


def _configure_cli_visuals(
    *,
    theme: AtlasThemeName,
    plain: bool,
    no_unicode: bool,
    no_animation: bool = False,
) -> None:
    """Apply global visual options before any command renders output."""

    global console
    configure_visuals(
        theme=theme,
        plain=plain,
        unicode=not no_unicode,
        motion=not no_animation,
    )
    console = themed_console()


def _settings() -> AtlasSettings:
    try:
        return load_config()
    except ConfigError as exc:
        console.print(
            f"[{ATLAS_ERROR_STYLE}]Config error:[/{ATLAS_ERROR_STYLE}] {exc}"
        )
        raise typer.Exit(2) from exc


def _engine(settings: AtlasSettings) -> YtdlpEngine:
    from atlas.engine import YtdlpEngine

    return YtdlpEngine(settings=settings, logger=logging.getLogger("atlas.engine"))


def _probe(engine: YtdlpEngine) -> MediaProbe:
    from atlas.engine import MediaProbe

    return MediaProbe(engine)


def _should_auto_launch_menu(*, no_menu: bool, json_output: bool) -> bool:
    if no_menu or json_output:
        return False
    return can_auto_launch_menu()


def _launch_menu(*, force: bool = False) -> None:
    if force and not has_interactive_tty():
        _handle_error(MenuUnavailable("Interactive menu requires a TTY."), verbose=False)
        raise typer.Exit(1)
    settings = _settings()
    try:
        run_interactive_menu(
            settings,
            _CliMenuActions(settings),
            console=console,
        )
    except MenuUnavailable as exc:
        _handle_error(exc, verbose=False)
        raise typer.Exit(1) from exc


def _bool_override(value: bool | None, default: bool) -> bool:
    return default if value is None else value


def _mirror_scope_policy(
    url: str,
    *,
    same_host_only: bool,
    same_domain_www: bool,
    include_subdomains: bool,
    span_hosts: bool,
    domains: str | None,
) -> tuple[bool, str | None]:
    selected = [same_host_only, same_domain_www, include_subdomains]
    if sum(1 for value in selected if value) > 1:
        raise AtlasError(
            "Choose only one mirror scope: --same-host-only, --same-domain-www, "
            "or --include-subdomains."
        )
    if same_host_only:
        return False, None
    if same_domain_www:
        return True, _default_domains_for_url(url) or domains
    if include_subdomains:
        host = _host_for_url(url)
        if host is None:
            return True, domains
        bare = host[4:] if host.startswith("www.") else host
        return True, bare
    return span_hosts, domains


def _default_domains_for_url(url: str) -> str | None:
    host = _host_for_url(url)
    if not host:
        return None
    if host.startswith("www."):
        bare = host[4:]
        return f"{bare},{host}"
    if _looks_like_ip_or_localhost(host):
        return host
    return f"{host},www.{host}"


def _host_for_url(url: str) -> str | None:
    host = urlparse(url).hostname
    return host.lower() if host else None


def _looks_like_ip_or_localhost(host: str) -> bool:
    if host == "localhost":
        return True
    return all(part.isdigit() for part in host.split(".") if part)


def _handle_error(exc: Exception, *, verbose: bool) -> None:
    if isinstance(exc, ValidationError):
        message = _validation_error_message(exc)
    else:
        message = " ".join(str(exc).split())
    output = Text("Error: ", style=ATLAS_ERROR_STYLE)
    output.append(message)
    console.print(output)
    hint = _recovery_hint(message)
    if hint:
        hint_output = Text("Hint: ", style=ATLAS_WARNING_STYLE)
        hint_output.append(hint, style=ATLAS_MUTED_STYLE)
        console.print(hint_output)
    if verbose and sys.exception() is exc:
        console.print_exception()


def _validation_error_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Invalid option"
    first = errors[0]
    message = str(first.get("msg") or "Invalid option")
    message = message.removeprefix("Value error, ")
    location = ".".join(str(part).replace("_", "-") for part in first.get("loc", ()))
    return f"{location}: {message}" if location else message


def _recovery_hint(message: str) -> str | None:
    lowered = message.lower()
    if "metalink" in lowered and "native file downloads" in lowered:
        return "Use --backend aria2 to expand the manifest, or --no-metalink to save it."
    if "aria2c is required for metalink" in lowered:
        return "Install aria2 with `brew install aria2`, or pass --no-metalink."
    if "aria2c is not installed" in lowered:
        return "Install aria2 with `brew install aria2`, or choose --backend native."
    if "curl_cffi" in lowered or "--impersonate" in lowered or "impersonate" in lowered:
        return "Install curl_cffi for impersonation support, or remove --impersonate."
    if "byte-range support" in lowered:
        return "Delete the partial file, retry with --no-continue, or force --backend aria2."
    if "checksum mismatch" in lowered:
        return "Verify the checksum string, delete the bad output, then retry."
    if "ffmpeg" in lowered or "ffprobe" in lowered:
        return "Install ffmpeg with `brew install ffmpeg`."
    return None


def _print_dry_run(result_opts: dict[str, object] | None) -> None:
    console.print(
        f"[{ATLAS_WARNING_STYLE}]Dry run[/{ATLAS_WARNING_STYLE}]: "
        "resolved yt-dlp options"
    )
    console.print_json(json.dumps(result_opts or {}, default=str, indent=2))


def _print_json(result_opts: dict[str, object] | None) -> None:
    console.print_json(json.dumps(result_opts or {}, default=str, indent=2))


def _archive_settings(
    *,
    settings: AtlasSettings,
    archive_path: Path | None,
    no_archive: bool,
    overwrite: bool = False,
) -> tuple[bool, Path | None]:
    if no_archive or overwrite:
        return False, None
    if archive_path:
        return True, archive_path
    return settings.archive, settings.archive_file


def _download_engine(
    *,
    selected: DownloadEngineChoice | None,
    aria2: bool | None,
) -> DownloadEngineChoice:
    if selected is not None:
        return selected
    if aria2 is False:
        return DownloadEngineChoice.native
    return DownloadEngineChoice.auto


def _use_aria2(settings: AtlasSettings, aria2: bool | None) -> bool:
    return settings.aria2 if aria2 is None else aria2


def _display_path(path: Path | str) -> str:
    value = str(path)
    home = str(Path.home())
    if value == home or value.startswith(f"{home}/"):
        return value.replace(home, "~", 1)
    return value


def _styled_path(path: Path | str) -> str:
    return f"[{ATLAS_PATH_STYLE}]{escape(_display_path(path))}[/{ATLAS_PATH_STYLE}]"


def _source_label(extractor: str | None) -> str:
    if not extractor:
        return "-"
    lowered = extractor.lower()
    if lowered.startswith("youtube"):
        return "YouTube"
    if lowered.startswith("rumble"):
        return "Rumble"
    return extractor


def _upload_date(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _views(value: int | None) -> str:
    return f"{value:,}" if value is not None else "-"


def _resolution_label(value: str | None) -> str:
    if not value:
        return "-"
    if value == "audio only":
        return "audio"
    if "x" in value:
        height = value.rsplit("x", 1)[-1]
        return f"{height}p" if height.isdigit() else value
    return value


def _codec_label(value: str | None) -> str:
    if not value:
        return "-"
    if value == "none":
        return "none"
    return value.split(".", 1)[0]


def _format_summary(fmt: FormatInfo | None) -> str:
    if fmt is None:
        return "-"
    format_id = fmt.format_id
    resolution = _resolution_label(fmt.resolution)
    fps = fmt.fps
    vcodec = _codec_label(fmt.vcodec)
    acodec = _codec_label(fmt.acodec)
    bitrate = fmt.tbr
    if vcodec != "none":
        fps_part = f"{fps:g}" if fps else ""
        return f"{format_id} {resolution}{fps_part} {vcodec}".strip()
    bitrate_part = f"{bitrate:g}k" if bitrate else ""
    return f"{format_id} {acodec} {bitrate_part}".strip()


def _format_summary_line(label: str, summary: str) -> Text:
    line = Text()
    line.append(label, style=ATLAS_MUTED_STYLE)
    line.append("  ")
    line.append(summary)
    return line


def _recommended_format_line(label: str, value: str) -> Text:
    line = Text()
    line.append("Recommended:", style=ATLAS_MUTED_STYLE)
    line.append(" ")
    line.append(label, style=ATLAS_ACTIVE_STYLE)
    line.append(f" {status_glyph('transition')} ", style=ATLAS_MUTED_STYLE)
    line.append(value, style=ATLAS_ACTIVE_STYLE)
    return line


def _recommended_formats(
    formats: Sequence[FormatInfo],
) -> tuple[FormatInfo | None, FormatInfo | None]:
    videos = sort_formats(filter_formats(formats, video_only=True), FormatSort.quality)
    audios = sort_formats(filter_formats(formats, audio_only=True), FormatSort.size)
    return (videos[0] if videos else None, audios[0] if audios else None)


def _print_smart_format_choices(formats: Sequence[FormatInfo]) -> None:
    catalog = MediaCapabilityCatalog.from_media_info(MediaInfo(formats=list(formats)))
    choices = MediaCapabilityResolver(catalog).all_profiles()
    if not choices:
        return
    profiles = Text()
    profiles.append("Profiles:", style=ATLAS_MUTED_STYLE)
    profiles.append(" " + visual_join(choice.label for choice in choices))
    console.print(profiles)
    table = Table(
        title=Text("Recommended profiles", style=ATLAS_TITLE_STYLE),
        box=table_box(),
        header_style=ATLAS_MUTED_STYLE,
    )
    for column in ("Profile", "Status", "Format", "Source", "Size"):
        table.add_column(column)
    for choice in choices:
        source_parts = [
            part
            for part in (
                choice.resolution,
                choice.video_codec,
                choice.audio_codec,
                choice.container if choice.container != "auto" else None,
            )
            if part
        ]
        table.add_row(
            choice.label,
            choice.status.value.replace("_", " "),
            choice.format_selector,
            visual_join(source_parts) or "-",
            format_bytes(choice.estimated_size),
            style=ATLAS_ACTIVE_STYLE if choice.label == "Best quality" else None,
        )
        for warning in choice.warnings:
            table.add_row("", "warning", escape(warning), "", "", style=ATLAS_WARNING_STYLE)
    console.print(table)


def _metadata_panel(title: str, rows: list[tuple[str, str]]) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    grid.add_column()
    for key, value in rows:
        grid.add_row(key, value)
    console.print(
        Panel(
            grid,
            title=Text(title, style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            expand=False,
        )
    )


def _planned_output_path(
    *,
    output_dir: Path,
    organize: OrganizeMode,
    uploader: str | None,
    upload_date: str | None,
    title: str | None,
    media_id: str | None,
    ext: str,
) -> Path:
    channel = _safe_segment(uploader or "unknown")
    date = _upload_date(upload_date)
    safe_date = "unknown" if date == "-" else date
    safe_title = _safe_segment(title or "untitled")[:120]
    safe_id = _safe_segment(media_id or "unknown")
    if organize == OrganizeMode.flat:
        return output_dir / f"{safe_date} - {safe_title} [{safe_id}].{ext}"
    if organize == OrganizeMode.channel:
        return output_dir / channel / f"{safe_title} [{safe_id}].{ext}"
    if organize == OrganizeMode.playlist:
        return output_dir / "playlist" / f"001 - {safe_title} [{safe_id}].{ext}"
    return output_dir / channel / f"{safe_date} - {safe_title} [{safe_id}].{ext}"


def _safe_segment(value: str) -> str:
    return "".join("-" if char in '/\\:*?"<>|' else char for char in value).strip() or "unknown"


def _output_preview(
    *,
    media: object,
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
    ext: str,
) -> Path | str:
    if options.filename_template:
        return plan.outtmpl
    return _planned_output_path(
        output_dir=options.output_dir,
        organize=options.organize,
        uploader=getattr(media, "uploader", None) or getattr(media, "channel", None),
        upload_date=getattr(media, "upload_date", None),
        title=getattr(media, "title", None),
        media_id=getattr(media, "id", None),
        ext=ext,
    )


def _audio_extension(options: AudioDownloadOptions) -> str:
    return "audio" if options.codec == AudioCodec.best else options.codec.value


def _engine_label(plan: DownloadPlan, requested: DownloadEngineChoice) -> str:
    if requested == DownloadEngineChoice.aria2:
        return "aria2c"
    if requested == DownloadEngineChoice.native:
        return "native"
    return "aria2c auto" if plan.use_aria2 else "native auto"


def _print_video_summary(
    media: object,
    options: VideoDownloadOptions,
    plan: DownloadPlan,
) -> None:
    container = plan.merge_output_format or options.container.value
    if options.container == Container.auto:
        container = container
    output = _output_preview(
        media=media,
        options=options,
        plan=plan,
        ext=plan.merge_output_format or "mkv",
    )
    _metadata_panel(
        "Download",
        [
            ("Title", escape(getattr(media, "title", None) or "-")),
            ("Source", _source_label(getattr(media, "extractor", None))),
            ("Quality", options.quality.value),
            ("Video Codec", options.video_codec.value),
            ("Format", escape(plan.format)),
            ("Container", container),
            ("Output", _styled_path(output)),
            ("Archive", "enabled" if options.archive else "disabled"),
            ("Engine", _engine_label(plan, options.download_engine)),
        ],
    )
    _print_smart_format_choices(getattr(media, "formats", []))


def _print_audio_summary(
    media: object,
    options: AudioDownloadOptions,
    plan: DownloadPlan,
) -> None:
    output = _output_preview(
        media=media,
        options=options,
        plan=plan,
        ext=_audio_extension(options),
    )
    _metadata_panel(
        "Audio Extraction",
        [
            ("Title", escape(getattr(media, "title", None) or "-")),
            ("Source", _source_label(getattr(media, "extractor", None))),
            ("Codec", options.codec.value),
            ("Format", escape(plan.format)),
            ("Metadata", "enabled" if options.embed_metadata else "disabled"),
            ("Artwork", "enabled" if options.embed_thumbnail else "disabled"),
            ("Output", _styled_path(output)),
            ("Engine", _engine_label(plan, options.download_engine)),
        ],
    )


def _print_saved_result(
    label: str,
    reporter: RichProgressReporter,
    output_dir: Path,
    *,
    archive_enabled: bool,
    archive_file: Path | None,
) -> None:
    console.print()
    if reporter.saved_paths:
        saved_paths = [Path(path) for path in reporter.saved_paths]
        primary_path = _primary_saved_result_path(saved_paths)
        hidden_paths = [path for path in saved_paths if path != primary_path]
        console.print(Text(f"{status_glyph('success')} {label}", style=ATLAS_SUCCESS_STYLE))
        console.print("Saved file:")
        console.print(_styled_path(primary_path))
        if hidden_paths:
            console.print(
                Text(
                    f"Technical files hidden: {len(hidden_paths)} stream file(s) merged.",
                    style=ATLAS_MUTED_STYLE,
                )
            )
        return
    console.print(Text("! No new file was downloaded", style=ATLAS_WARNING_STYLE))
    if archive_enabled:
        console.print("This URL may already be recorded in the download archive.")
        if archive_file:
            console.print(f"Archive: {_styled_path(archive_file)}")
        console.print(
            "Use [bold]--overwrite[/bold] or [bold]--no-archive[/bold] to download it again."
        )
    else:
        console.print("No saved path was reported by the downloader.")
        console.print(f"Output: {_styled_path(output_dir)}")


def _primary_saved_result_path(saved_paths: Sequence[Path]) -> Path:
    non_temporary = [path for path in saved_paths if not _looks_like_temporary_stream(path)]
    if non_temporary:
        return non_temporary[-1]
    return saved_paths[-1]


def _looks_like_temporary_stream(path: Path) -> bool:
    return bool(_TEMP_STREAM_RE.search(path.name))


def _video_archive_retry_options(options: VideoDownloadOptions) -> VideoDownloadOptions | None:
    if not options.archive:
        return None
    return options.model_copy(update={"archive": False, "archive_file": None})


def _audio_archive_retry_options(options: AudioDownloadOptions) -> AudioDownloadOptions | None:
    if not options.archive:
        return None
    return options.model_copy(update={"archive": False, "archive_file": None})


def _maybe_print_archive_retry(
    options: VideoDownloadOptions | AudioDownloadOptions,
) -> None:
    if options.quiet:
        return
    console.print()
    console.print(
        Text(
            f"{status_glyph('warning')} Archive mismatch - saved file missing. "
            "Re-downloading once.",
            style=ATLAS_WARNING_STYLE,
        )
    )


def _active_progress_mode(options: object) -> ProgressMode:
    quiet = bool(getattr(options, "quiet", False))
    json_output = bool(getattr(options, "json_output", False))
    progress_mode = getattr(options, "progress_mode", ProgressMode.auto)
    if not isinstance(progress_mode, ProgressMode):
        progress_mode = ProgressMode(str(progress_mode))
    return resolve_progress_mode(
        progress_mode,
        console=console,
        quiet=quiet,
        json_output=json_output,
    )


def _rich_progress_reporter(
    progress_mode: ProgressMode,
    kind: HubKind,
    *,
    work_context: WorkPanelContext | None = None,
) -> RichProgressReporter:
    return RichProgressReporter(
        console,
        mode=progress_mode,
        kind=kind,
        work_context=work_context,
        alternate_screen=_alternate_screen_enabled(progress_mode),
    )


def _alternate_screen_enabled(progress_mode: ProgressMode) -> bool:
    return should_use_alternate_screen(
        progress_mode,
        console=console,
        plain=visual_options().plain,
    )


def _media_work_context(
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
) -> WorkPanelContext:
    is_audio = isinstance(options, AudioDownloadOptions)
    kind = HubKind.audio if is_audio else HubKind.video
    operation = "Extract audio" if is_audio else "Download video"
    steps = (
        ("Download audio", "Embed metadata", "Add artwork", "Finalize")
        if is_audio
        else ("Download video", "Merge video/audio", "Embed metadata", "Add thumbnail", "Finalize")
    )
    return WorkPanelContext(
        queue_count=1,
        kind=kind,
        operation=operation,
        source=_url_source_label(options.url),
        quality=_media_quality_label(options, plan),
        output=_display_path(plan.output_dir),
        safety_badges=_media_safety_badges(options, plan),
        steps=steps,
    )


def _file_work_context(
    options: FileDownloadOptions,
    *,
    backend: str,
    output: Path | None = None,
) -> WorkPanelContext:
    badges = ["resume"]
    if options.adaptive_plan:
        badges.append(f"adaptive {options.adaptive_plan.strategy}")
    if backend == "aria2":
        badges.append("aria2")
    if options.checksum:
        badges.append("checksum")
    if options.overwrite:
        badges.append("overwrite")
    return WorkPanelContext(
        queue_count=1,
        operation="Download",
        source=_url_source_label(options.url),
        engine=backend,
        output=_display_path(output or options.output_dir),
        mode_label="adaptive" if options.adaptive_plan else "fixed",
        safety_badges=tuple(badges),
    )


def _site_work_context(
    options: SiteDownloadOptions,
    *,
    backend: str,
    output: Path | None = None,
) -> WorkPanelContext:
    badges = ["directory" if isinstance(options, DirectoryMirrorOptions) else "recursive"]
    if options.adaptive_plan:
        badges.append(f"adaptive {options.adaptive_plan.politeness.value}")
    if not options.span_hosts:
        badges.append("single-host")
    return WorkPanelContext(
        queue_count=1,
        operation="Download",
        source=_url_source_label(options.url),
        engine=backend,
        output=_display_path(output or options.output_dir),
        mode_label="adaptive" if options.adaptive_plan else "fixed",
        safety_badges=tuple(badges),
    )


def _batch_work_context(
    *,
    queue_count: int | None,
    concurrency: int,
    allow_sites: bool,
    allow_dirs: bool,
    output_dir: Path | None = None,
    adaptive_plan: AdaptiveDownloadPlan | None = None,
) -> WorkPanelContext:
    badges = [f"concurrency {concurrency}"]
    if adaptive_plan is not None:
        badges.extend(
            [
                f"adaptive {adaptive_plan.politeness.value}",
                f"per-host {adaptive_plan.per_host_concurrency}",
                f"segments {adaptive_plan.per_file_segments}",
            ]
        )
        if adaptive_plan.speed_limit:
            badges.append(f"speed {adaptive_plan.speed_limit}")
    badges.append("sites allowed" if allow_sites else "sites skipped")
    badges.append("dirs allowed" if allow_dirs else "dirs skipped")
    return WorkPanelContext(
        queue_count=queue_count,
        operation="Batch Download",
        output=_display_path(output_dir) if output_dir is not None else None,
        mode_label="adaptive" if adaptive_plan is not None else f"concurrency {concurrency}",
        backends=("yt-dlp", "aria2c", "native", "wget2"),
        safety_badges=tuple(badges),
    )


def _url_source_label(url: str) -> str:
    host = urlparse(url).hostname
    return host or url


def _media_quality_label(
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
) -> str:
    container = plan.merge_output_format or getattr(options, "format", None) or "auto"
    if isinstance(options, VideoDownloadOptions):
        return visual_join((options.resolution.value, str(container)))
    codec = options.codec.value if isinstance(options, AudioDownloadOptions) else "best"
    return visual_join((codec, str(container)))


def _adaptive_batch_runtime_scheduler(
    plan: AdaptiveDownloadPlan,
) -> AdaptiveScheduler:
    min_concurrency = max(1, min(plan.global_min_concurrency, plan.queue_concurrency))
    scheduler = AdaptiveScheduler(
        max_concurrency=plan.queue_concurrency,
        per_host_concurrency=plan.per_host_concurrency,
        politeness=plan.politeness,
        min_concurrency=min_concurrency,
    )
    scheduler.current_concurrency = max(
        min_concurrency,
        min(plan.queue_concurrency, scheduler.global_max_concurrency),
    )
    scheduler.current_speed_limit = plan.speed_limit
    return scheduler


def _adaptive_work_item_by_url(
    plan: AdaptiveDownloadPlan | None,
) -> dict[str, WorkItem]:
    if plan is None:
        return {}
    items: dict[str, WorkItem] = {}
    for item in plan.work_items:
        items.setdefault(item.url, item)
        if item.final_url:
            items.setdefault(item.final_url, item)
    return items


def _adaptive_progress_updates(
    *,
    entry: BatchEntry,
    event: ProgressEvent | None,
    adaptive_plan: AdaptiveDownloadPlan | None,
    adaptive_scheduler: AdaptiveScheduler | None,
    adaptive_items_by_url: Mapping[str, WorkItem] | None = None,
) -> dict[str, object]:
    if adaptive_plan is None:
        return {}
    item = (adaptive_items_by_url or _adaptive_work_item_by_url(adaptive_plan)).get(entry.url)
    updates: dict[str, object] = {
        "queue_concurrency": (
            adaptive_scheduler.current_concurrency
            if adaptive_scheduler is not None
            else adaptive_plan.queue_concurrency
        ),
        "per_host_concurrency": adaptive_plan.per_host_concurrency,
        "per_file_segments": adaptive_plan.per_file_segments,
        "max_total_connections": adaptive_plan.max_total_connections,
        "max_per_host_connections": adaptive_plan.max_per_host_connections,
        "max_active_postprocessors": adaptive_plan.max_active_postprocessors,
        "scheduler_decision": adaptive_plan.strategy,
    }
    if adaptive_plan.speed_limit:
        updates["speed_limit"] = adaptive_plan.speed_limit
    if item is None:
        return updates

    bucket = item.bucket
    size_class = item.size_class
    decision = item.scheduler_decision or adaptive_plan.strategy
    reclassified_from: str | None = None
    total_bytes = event.total_bytes if event is not None else None
    if (
        total_bytes is not None
        and item.size_class == FileSizeClass.unknown
    ):
        size_class = classify_file_size(total_bytes)
        bucket = WorkBucket(size_class.value)
        if size_class != FileSizeClass.unknown:
            reclassified_from = FileSizeClass.unknown.value
            decision = f"reclassified unknown to {bucket.value}; {adaptive_plan.strategy}"
            if adaptive_scheduler is not None:
                adaptive_scheduler.record_transfer_classification(size_class)
                updates["queue_concurrency"] = adaptive_scheduler.current_concurrency

    updates.update(
        {
            "estimated_bytes": item.content_length,
            "size_class": size_class,
            "work_bucket": bucket,
            "selected_backend": item.selected_backend or adaptive_plan.backend,
            "priority": item.priority,
            "scheduler_decision": decision,
        }
    )
    if item.recursion_depth is not None:
        updates["recursion_depth"] = item.recursion_depth
    if reclassified_from is not None:
        updates["reclassified_from"] = reclassified_from
    return {key: value for key, value in updates.items() if value is not None}


def _media_safety_badges(
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
) -> tuple[str, ...]:
    badges: list[str] = []
    badges.append("playlist enabled" if options.playlist else "single video")
    if plan.noplaylist:
        badges.append("playlist disabled")
    if options.browser_cookies or options.cookies_file:
        badges.append("cookies active")
    if plan.use_aria2:
        badges.append("aria2")
        badges.append("merge pending")
    return tuple(badges)


def _emit_media_startup_phases(
    reporter: RichProgressReporter,
    options: VideoDownloadOptions | AudioDownloadOptions,
    plan: DownloadPlan,
) -> None:
    if not hasattr(reporter, "hook"):
        return
    kind = HubKind.audio if isinstance(options, AudioDownloadOptions) else HubKind.video
    title = "metadata"
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="done",
            phase=ProgressPhase.probe,
            kind=kind,
            url=options.url,
            title=title,
            message="probe complete",
        )
    )
    engine = EngineKind.aria2 if plan.use_aria2 else EngineKind.ytdlp
    title = "aria2c external downloader" if plan.use_aria2 else "yt-dlp downloader"
    if plan.use_aria2 and kind == HubKind.audio:
        message = "temporary .part files are normal until finalize"
    elif plan.use_aria2:
        message = "temporary .part files are normal until merge"
    else:
        message = "starting transfer"
    reporter.hook(
        ProgressEvent(
            engine=engine,
            status="running",
            phase=ProgressPhase.download,
            kind=kind,
            url=options.url,
            title=title,
            message=message,
        )
    )


def _batch_progress_reporter(
    *,
    concurrency: int,
    progress_mode: ProgressMode,
    total: int | None = None,
    work_context: WorkPanelContext | None = None,
    operator_controller: BatchOperatorController | None = None,
) -> BatchProgressReporter:
    return BatchProgressReporter(
        console,
        total=total,
        concurrency=concurrency,
        mode=progress_mode,
        work_context=work_context,
        operator_controller=operator_controller,
        alternate_screen=_alternate_screen_enabled(progress_mode),
    )


def _media_progress_hooks(
    reporter: RichProgressReporter,
    _kind: HubKind,
) -> list[ProgressHook]:
    return [create_progress_hook(reporter)]


def _media_postprocessor_hooks(
    reporter: RichProgressReporter,
    _kind: HubKind,
) -> list[ProgressHook]:
    return [create_postprocessor_hook(reporter)]


def _print_backend_result(label: str, message: str | None, fallback_output: Path) -> None:
    console.print()
    console.print(Text(f"{status_glyph('success')} {label}", style=ATLAS_SUCCESS_STYLE))
    console.print(message or f"Saved under {fallback_output}")


def _print_file_summary(
    options: FileDownloadOptions,
    backend: str,
    output: Path,
    *,
    preview: dict[str, object] | None = None,
) -> None:
    rows = [
        ("URL", escape(options.url)),
        ("Backend", backend),
        ("Output", _styled_path(output)),
        ("Resume", "enabled" if options.continue_download else "disabled"),
        ("Overwrite", "enabled" if options.overwrite else "disabled"),
        ("Timestamping", "enabled" if options.timestamping else "disabled"),
        (
            "Metalink",
            "forced" if options.force_metalink else "auto" if options.metalink else "disabled",
        ),
        ("HTTP Method", options.method),
        (
            "Filename",
            "content-disposition"
            if options.content_disposition
            else "url/redirect"
            if options.trust_server_names
            else "url",
        ),
    ]
    probe = _probe_summary_from_preview(preview)
    if probe:
        content_length = probe.get("content_length")
        if isinstance(content_length, int):
            rows.append(("Size", format_bytes(content_length)))
        content_type = probe.get("content_type")
        if isinstance(content_type, str):
            rows.append(("Type", escape(content_type)))
        rows.append(("Ranges", "supported" if probe.get("supports_ranges") else "not reported"))
        if probe.get("redirected"):
            final_url = probe.get("final_url")
            redirect = escape(str(final_url)) if final_url else "yes"
            rows.append(("Redirect", redirect))
    adaptive = _adaptive_summary_from_preview(preview)
    if adaptive:
        rows.append(("Adaptive", escape(str(adaptive.get("strategy") or "enabled"))))
        rows.append(("Queue", str(adaptive.get("queue_concurrency") or "-")))
        rows.append(("Segments", str(adaptive.get("per_file_segments") or "-")))
        rows.append(("Per host", str(adaptive.get("per_host_concurrency") or "-")))
    if options.checksum:
        rows.append(("Checksum", options.checksum.split(":", 1)[0]))
    if options.user_agent:
        rows.append(("User Agent", escape(options.user_agent)))
    if options.headers:
        rows.append(("Headers", str(len(options.headers))))
    _metadata_panel("File Download", rows)


def _probe_summary_from_preview(preview: dict[str, object] | None) -> dict[str, object] | None:
    if not preview:
        return None
    summary = preview.get("summary")
    if not isinstance(summary, dict):
        return None
    probe = summary.get("probe")
    return probe if isinstance(probe, dict) else None


def _adaptive_summary_from_preview(preview: dict[str, object] | None) -> dict[str, object] | None:
    if not preview:
        return None
    summary = preview.get("summary")
    if not isinstance(summary, dict):
        return None
    adaptive = summary.get("adaptive")
    return adaptive if isinstance(adaptive, dict) else None


def _print_site_summary(
    options: SiteDownloadOptions,
    backend: str,
    *,
    preview: dict[str, object] | None = None,
) -> None:
    rows = [
        ("URL", escape(options.url)),
        ("Backend", backend),
        ("Depth", str(options.depth)),
        ("Assets", "enabled" if options.page_requisites else "disabled"),
        ("Convert", "enabled" if options.convert_links else "disabled"),
        ("Span hosts", "enabled" if options.span_hosts else "disabled"),
        ("Robots", "respected" if options.robots else "ignored"),
        ("Sitemaps", "followed" if options.follow_sitemaps else "ignored"),
        ("No parent", "enabled" if options.no_parent else "disabled"),
        ("Resume", "enabled" if options.continue_download else "disabled"),
        ("Timestamping", "enabled" if options.timestamping else "disabled"),
        ("Threads", str(options.max_threads)),
        ("Tries", str(options.tries)),
        ("Wait retry", f"{options.waitretry:g}s"),
        ("Redirects", str(options.max_redirect)),
        ("Mode", "spider/check" if options.spider else "mirror"),
        ("Output", _styled_path(options.output_dir)),
    ]
    layout = [
        label
        for label, enabled in (
            ("directories", options.directories),
            ("host-dirs", options.host_directories),
            ("protocol-dirs", options.protocol_directories),
            ("adjust-ext", options.adjust_extension),
        )
        if enabled
    ]
    if layout:
        rows.append(("Layout", ", ".join(layout)))
    if isinstance(options, DirectoryMirrorOptions):
        rows.append(
            (
                "If-Modified-Since",
                "enabled" if options.if_modified_since else "disabled",
            )
        )
    if options.user_agent:
        rows.append(("User Agent", escape(options.user_agent)))
    if (
        options.https_only
        or options.https_enforce
        or options.http2_only
        or options.hsts is not None
        or options.inet4_only
        or options.inet6_only
        or options.bind_address
    ):
        rows.append(
            (
                "Transport",
                ", ".join(
                    label
                    for label, enabled in (
                        ("https-only", options.https_only),
                        ("https-enforce", bool(options.https_enforce)),
                        ("http2-only", options.http2_only),
                        ("hsts", options.hsts is True),
                        ("no-hsts", options.hsts is False),
                        ("ipv4", options.inet4_only),
                        ("ipv6", options.inet6_only),
                        ("bind-address", bool(options.bind_address)),
                    )
                    if enabled
                ),
            )
        )
    if options.input_file:
        rows.append(
            (
                "Input",
                f"{_styled_path(options.input_file)}"
                + (" (file-only)" if options.input_file_only else ""),
            )
        )
    if options.warc_file:
        rows.append(("WARC", _styled_path(options.warc_file)))
    bounds = [
        label
        for label, value in (
            (f"max-files {options.max_files}", options.max_files),
            (f"max-total-size {options.max_total_size}", options.max_total_size),
            (f"max-runtime {options.max_runtime:g}s", options.max_runtime),
        )
        if value is not None
    ]
    if bounds:
        rows.append(("Bounds", ", ".join(bounds)))
    filters = [
        label
        for label, value in (
            ("accept", options.accept),
            ("reject", options.reject),
            ("domains", options.domains),
            ("exclude-domains", options.exclude_domains),
            ("include-dirs", options.include_directories),
            ("exclude-dirs", options.exclude_directories),
            ("accept-regex", options.accept_regex),
            ("reject-regex", options.reject_regex),
            ("mime", options.filter_mime_type),
            ("filter-urls", "on" if options.filter_urls else None),
            ("follow-tags", options.follow_tags),
            ("ignore-tags", options.ignore_tags),
        )
        if value
    ]
    if filters:
        rows.append(("Filters", ", ".join(filters)))
    adaptive = _adaptive_summary_from_preview(preview)
    if adaptive:
        rows.append(("Adaptive", escape(str(adaptive.get("strategy") or "enabled"))))
        rows.append(("Queue", str(adaptive.get("queue_concurrency") or "-")))
        rows.append(("Per host", str(adaptive.get("per_host_concurrency") or "-")))
    panel_title = (
        "Directory Mirror" if isinstance(options, DirectoryMirrorOptions) else "Site Mirror"
    )
    _metadata_panel(panel_title, rows)


def _print_wget2_stats(stats: dict[str, object] | None) -> None:
    if not stats:
        return
    rows: list[tuple[str, str]] = []
    summary = stats.get("summary")
    if isinstance(summary, dict):
        site = summary.get("site")
        if isinstance(site, dict):
            urls = site.get("urls")
            failures = site.get("failures")
            redirects = site.get("redirects")
            bytes_downloaded = site.get("downloaded_bytes")
            if isinstance(urls, int):
                rows.append(("Site URLs", str(urls)))
            if isinstance(failures, int):
                rows.append(("Failures", str(failures)))
            if isinstance(redirects, int):
                rows.append(("Redirects", str(redirects)))
            if isinstance(bytes_downloaded, int):
                rows.append(("Downloaded", format_bytes(bytes_downloaded)))
        server = summary.get("server")
        if isinstance(server, dict):
            hosts = server.get("hosts")
            hsts_hosts = server.get("hsts_hosts")
            csp_hosts = server.get("csp_hosts")
            https_hosts = server.get("https_hosts")
            http_hosts = server.get("http_hosts")
            hosts_without_hsts = server.get("hosts_without_hsts")
            hosts_without_csp = server.get("hosts_without_csp")
            mixed_scheme_hosts = server.get("mixed_scheme_hosts")
            if isinstance(hosts, int):
                rows.append(("Hosts", str(hosts)))
            if isinstance(hsts_hosts, int):
                rows.append(("HSTS Hosts", str(hsts_hosts)))
            if isinstance(csp_hosts, int):
                rows.append(("CSP Hosts", str(csp_hosts)))
            if isinstance(https_hosts, int):
                rows.append(("HTTPS Hosts", str(https_hosts)))
            if isinstance(http_hosts, int):
                rows.append(("HTTP Hosts", str(http_hosts)))
            if isinstance(hosts_without_hsts, list) and hosts_without_hsts:
                rows.append(("Missing HSTS", ", ".join(map(str, hosts_without_hsts))))
            if isinstance(hosts_without_csp, list) and hosts_without_csp:
                rows.append(("Missing CSP", ", ".join(map(str, hosts_without_csp))))
            if isinstance(mixed_scheme_hosts, list) and mixed_scheme_hosts:
                rows.append(("Mixed Schemes", ", ".join(map(str, mixed_scheme_hosts))))
        dns = summary.get("dns")
        if isinstance(dns, dict):
            lookups = dns.get("lookups")
            failures = dns.get("failures")
            average_lookup = dns.get("average_lookup_time_ms")
            addresses = dns.get("addresses")
            if isinstance(lookups, int):
                rows.append(("DNS Lookups", str(lookups)))
            if isinstance(addresses, int):
                rows.append(("DNS Addresses", str(addresses)))
            if isinstance(failures, int):
                rows.append(("DNS Failures", str(failures)))
            if isinstance(average_lookup, int):
                rows.append(("Avg DNS", f"{average_lookup} ms"))
        tls = summary.get("tls")
        if isinstance(tls, dict):
            connections = tls.get("connections")
            versions = tls.get("versions")
            resumed = tls.get("resumed_connections")
            average_tls = tls.get("average_tls_time_ms")
            if isinstance(connections, int):
                rows.append(("TLS Connections", str(connections)))
            if isinstance(versions, dict) and versions:
                rows.append(("TLS Versions", ", ".join(f"{k}:{v}" for k, v in versions.items())))
            if isinstance(resumed, int):
                rows.append(("TLS Resumed", str(resumed)))
            if isinstance(average_tls, int):
                rows.append(("Avg TLS", f"{average_tls} ms"))
        ocsp = summary.get("ocsp")
        if isinstance(ocsp, dict):
            stapled_hosts = ocsp.get("stapled_hosts")
            revoked = ocsp.get("revoked_responses")
            ignored = ocsp.get("ignored_responses")
            revoked_hosts = ocsp.get("revoked_hosts")
            if isinstance(stapled_hosts, int):
                rows.append(("OCSP Stapled Hosts", str(stapled_hosts)))
            if isinstance(revoked, int):
                rows.append(("OCSP Revoked", str(revoked)))
            if isinstance(ignored, int):
                rows.append(("OCSP Ignored", str(ignored)))
            if isinstance(revoked_hosts, list) and revoked_hosts:
                rows.append(("OCSP Revoked Hosts", ", ".join(map(str, revoked_hosts))))
    for label, payload in stats.items():
        if label == "summary":
            continue
        if not isinstance(payload, dict):
            continue
        row_count = payload.get("rows")
        line_count = payload.get("lines")
        if isinstance(row_count, list):
            rows.append((label.title(), f"{len(row_count)} rows"))
        elif isinstance(line_count, list):
            rows.append((label.title(), f"{len(line_count)} lines"))
    if rows:
        _metadata_panel("Wget2 Stats", rows)


def _print_backend_plan(
    plan: dict[str, object],
    *,
    json_output: bool,
    explain: bool = False,
) -> None:
    if json_output:
        _print_json(plan)
        return
    _print_backend_plan_preview(plan, explain=explain)


def _print_backend_plan_preview(plan: dict[str, object], *, explain: bool) -> None:
    label = "Explain" if explain else "Dry Run"
    route = _mapping_value(plan, "route")
    summary = _mapping_value(plan, "summary")
    session = _mapping_value(plan, "session")
    adaptive = _mapping_value(summary, "adaptive")
    scheduler_policy = _mapping_value(session, "scheduler_policy")

    kind = _string_value(route.get("kind") or summary.get("mirror_kind") or session.get("intent"))
    engine = _string_value(route.get("engine") or summary.get("backend") or "-")
    output = _string_value(plan.get("output") or route.get("output_dir") or "-")
    strategy = _string_value(
        adaptive.get("strategy")
        or scheduler_policy.get("strategy")
        or summary.get("backend_reason")
        or "fixed backend plan"
    )
    source = _string_value(route.get("url") or session.get("source") or "-")

    view = SmartSessionView(title="atlas")
    console.print(
        view.plan_preview(
            heading=f"{label} Plan",
            fields=(
                ViewField("Source", source, "path"),
                ViewField("Mode", kind or "-"),
                ViewField("Engine", engine or "-"),
                ViewField("Output", _display_path(output), "path"),
                ViewField("Strategy", strategy, "active"),
                ViewField("Full plan", "rerun with --json for the stable machine payload", "muted"),
            ),
            sections=_backend_plan_sections(
                route=route,
                summary=summary,
                session=session,
                adaptive=adaptive,
                scheduler_policy=scheduler_policy,
            ),
            actions=("Start", "Customize", "Dry run", "Save manifest"),
        )
    )

    args = plan.get("args")
    if isinstance(args, list) and args:
        command = shlex.join(str(arg) for arg in args)
        console.print(
            view.preview_panel(
                title="Equivalent Backend Command",
                content=command,
                syntax="bash",
            )
        )


def _backend_plan_sections(
    *,
    route: dict[str, object],
    summary: dict[str, object],
    session: dict[str, object],
    adaptive: dict[str, object],
    scheduler_policy: dict[str, object],
) -> dict[str, tuple[ViewField, ...]]:
    sections: dict[str, tuple[ViewField, ...]] = {}

    scope_rows = _plan_rows(
        (
            ("Kind", route.get("kind") or summary.get("mirror_kind")),
            ("Detected", session.get("detected_kind")),
            ("Depth", summary.get("depth")),
            ("No parent", summary.get("no_parent")),
            ("Domains", summary.get("domains")),
            ("Span hosts", summary.get("span_hosts")),
            ("Directories", summary.get("directories")),
        )
    )
    if scope_rows:
        sections["Scope"] = scope_rows

    html_rows = _plan_rows(
        (
            ("Keep HTML", summary.get("keep_html")),
            ("Convert links", summary.get("convert_links")),
            ("Adjust extension", summary.get("adjust_extension")),
            ("Page requisites", summary.get("assets")),
            ("Follow sitemaps", summary.get("follow_sitemaps")),
            ("Robots", summary.get("robots")),
        )
    )
    if html_rows:
        sections["HTML"] = html_rows

    network_rows = _plan_rows(
        (
            ("Method", summary.get("method")),
            ("Wait", _seconds_value(summary.get("wait"))),
            ("Random wait", summary.get("random_wait")),
            ("Timeout", _seconds_value(summary.get("timeout"))),
            ("Tries", summary.get("tries") or summary.get("max_tries")),
            ("Continue", summary.get("resume")),
            ("Overwrite", summary.get("overwrite")),
        )
    )
    if network_rows:
        sections["Network"] = network_rows

    scheduler_rows = _plan_rows(
        (
            ("Mode", scheduler_policy.get("mode") or ("adaptive" if adaptive else None)),
            ("Queue", adaptive.get("queue_concurrency")),
            ("Per host", adaptive.get("per_host_concurrency")),
            ("Connections", adaptive.get("max_total_connections") or summary.get("connections")),
            ("Segments", adaptive.get("per_file_segments") or summary.get("splits")),
            ("Backend", adaptive.get("backend") or summary.get("backend")),
            ("Decision", adaptive.get("strategy") or scheduler_policy.get("strategy")),
        ),
        active_labels={"Decision"},
    )
    if scheduler_rows:
        sections["Scheduler"] = scheduler_rows

    safety_values: list[str] = []
    safety = route.get("safety")
    if isinstance(safety, list):
        safety_values.extend(str(value) for value in safety)
    warnings = summary.get("warnings")
    if isinstance(warnings, list):
        safety_values.extend(str(value) for value in warnings)
    safety_notes = adaptive.get("safety_notes")
    if isinstance(safety_notes, list):
        safety_values.extend(str(value) for value in safety_notes[:3] if value)
    if safety_values:
        sections["Safety"] = tuple(
            ViewField(f"Rule {index}", value, "success" if index == 1 else "info")
            for index, value in enumerate(safety_values, start=1)
        )

    return sections


def _plan_rows(
    rows: Sequence[tuple[str, object]],
    *,
    active_labels: set[str] | None = None,
) -> tuple[ViewField, ...]:
    active_labels = active_labels or set()
    fields: list[ViewField] = []
    for label, value in rows:
        rendered = _render_plan_scalar(value)
        if rendered is None:
            continue
        state = "active" if label in active_labels else _plan_state(value)
        fields.append(ViewField(label, rendered, state))
    return tuple(fields)


def _mapping_value(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _string_value(value: object) -> str:
    return str(value) if value is not None else ""


def _seconds_value(value: object) -> object:
    if value is None:
        return None
    return f"{value}s"


def _render_plan_scalar(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set)):
        if not value:
            return None
        return ", ".join(str(item) for item in value)
    return str(value)


def _plan_state(value: object) -> str:
    if isinstance(value, bool):
        return "success" if value else "muted"
    return "info"


def _print_backend_command_plan(plan: BackendCommandPlan) -> None:
    _metadata_panel(
        "Advanced Backend",
        [
            ("Tool", plan.display_name),
            ("Command", escape(shlex.join(plan.command))),
            ("Working Dir", _styled_path(plan.cwd)),
            ("Mode", "raw pass-through"),
            ("Safety", "; ".join(plan.safety)),
        ],
    )


def _run_backend_passthrough(
    tool: BackendTool,
    args: list[str],
    *,
    dry_run: bool,
    json_output: bool,
    quiet: bool,
    timeout: float | None,
    verbose: bool,
    backend_help: bool = False,
) -> None:
    configure_logging(verbose)
    user_args = ["--help"] if backend_help and not args else args
    if not user_args:
        _handle_error(
            AtlasError(
                f"Pass {tool.value} arguments after --, for example: atlas {tool.name} -- --help"
            ),
            verbose=verbose,
        )
        raise typer.Exit(1)
    try:
        plan = plan_backend_command(tool, user_args)
        if dry_run:
            payload = backend_plan_as_dict(plan)
            _print_json(payload) if json_output else _print_backend_command_plan(plan)
            return
        if not quiet and not json_output:
            _print_backend_command_plan(plan)
        if json_output:
            result = run_backend_command(plan, timeout=timeout, stream=False)
            console.print_json(
                json.dumps(
                    {
                        "plan": backend_plan_as_dict(plan),
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                    default=str,
                )
            )
        elif quiet:
            result = run_backend_command(plan, timeout=timeout, stream=False)
        else:
            result = run_backend_command(
                plan,
                timeout=timeout,
                stream=True,
                on_line=lambda line: console.print(escape(line)),
            )
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or (
                f"{tool.value} exited {result.returncode}"
            )
            if json_output:
                raise typer.Exit(result.returncode)
            _handle_error(AtlasError(message), verbose=verbose)
            raise typer.Exit(result.returncode)
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc


def _resolve_playlist_kind(kind: BatchKind | None, *, yes: bool, quiet: bool) -> BatchKind:
    if kind is not None:
        if kind == BatchKind.auto:
            msg = "Playlist type must be video or audio."
            raise AtlasError(msg)
        return kind
    if yes:
        return BatchKind.video
    if quiet:
        msg = "--type is required for atlas playlist when --quiet is used."
        raise AtlasError(msg)
    answer = typer.prompt("Download playlist as video or audio?", default="video").strip().lower()
    if answer in {"v", "video"}:
        return BatchKind.video
    if answer in {"a", "audio"}:
        return BatchKind.audio
    msg = "Playlist type must be video or audio."
    raise AtlasError(msg)


def _run_video_download(settings: AtlasSettings, options: VideoDownloadOptions) -> list[Path]:
    try:
        plan = SmartPlanner(settings).plan_video(options)
        engine = _engine(settings)
        if options.dry_run:
            result = engine.download_video(options)
            _print_json(result.ydl_opts) if options.json_output else _print_dry_run(result.ydl_opts)
            return []
        ensure_download_dependencies(settings, BatchKind.video, plan)
        media = _probe(engine).probe(
            InfoOptions(
                url=options.url,
                browser_cookies=options.browser_cookies,
                cookies_file=options.cookies_file,
                playlist=options.playlist,
                verbose=options.verbose,
            )
        )
        if not options.quiet:
            _print_video_summary(media, options, plan)
        progress_mode = _active_progress_mode(options)
        with _rich_progress_reporter(
            progress_mode,
            HubKind.video,
            work_context=_media_work_context(options, plan),
        ) as reporter:
            _emit_media_startup_phases(reporter, options, plan)
            engine.download_video(
                options,
                progress_hooks=_media_progress_hooks(reporter, HubKind.video),
                postprocessor_hooks=_media_postprocessor_hooks(reporter, HubKind.video),
            )
        retry_options = _video_archive_retry_options(options) if not reporter.saved_paths else None
        if retry_options is not None:
            _maybe_print_archive_retry(options)
            with _rich_progress_reporter(
                progress_mode,
                HubKind.video,
                work_context=_media_work_context(retry_options, plan),
            ) as retry_reporter:
                _emit_media_startup_phases(retry_reporter, retry_options, plan)
                engine.download_video(
                    retry_options,
                    progress_hooks=_media_progress_hooks(retry_reporter, HubKind.video),
                    postprocessor_hooks=_media_postprocessor_hooks(retry_reporter, HubKind.video),
                )
            reporter = retry_reporter
        if not options.quiet:
            _print_saved_result(
                "Download complete",
                reporter,
                options.output_dir,
                archive_enabled=retry_options is None and options.archive,
                archive_file=options.archive_file,
            )
        return [Path(path) for path in reporter.saved_paths]
    except AtlasError as exc:
        _handle_error(exc, verbose=options.verbose)
        raise typer.Exit(1) from exc


def _run_audio_download(settings: AtlasSettings, options: AudioDownloadOptions) -> list[Path]:
    try:
        plan = SmartPlanner(settings).plan_audio(options)
        engine = _engine(settings)
        if options.dry_run:
            result = engine.download_audio(options)
            _print_json(result.ydl_opts) if options.json_output else _print_dry_run(result.ydl_opts)
            return []
        ensure_download_dependencies(settings, BatchKind.audio, plan)
        media = _probe(engine).probe(
            InfoOptions(
                url=options.url,
                browser_cookies=options.browser_cookies,
                cookies_file=options.cookies_file,
                playlist=options.playlist,
                verbose=options.verbose,
            )
        )
        if not options.quiet:
            _print_audio_summary(media, options, plan)
        progress_mode = _active_progress_mode(options)
        with _rich_progress_reporter(
            progress_mode,
            HubKind.audio,
            work_context=_media_work_context(options, plan),
        ) as reporter:
            _emit_media_startup_phases(reporter, options, plan)
            engine.download_audio(
                options,
                progress_hooks=_media_progress_hooks(reporter, HubKind.audio),
                postprocessor_hooks=_media_postprocessor_hooks(reporter, HubKind.audio),
            )
        retry_options = _audio_archive_retry_options(options) if not reporter.saved_paths else None
        if retry_options is not None:
            _maybe_print_archive_retry(options)
            with _rich_progress_reporter(
                progress_mode,
                HubKind.audio,
                work_context=_media_work_context(retry_options, plan),
            ) as retry_reporter:
                _emit_media_startup_phases(retry_reporter, retry_options, plan)
                engine.download_audio(
                    retry_options,
                    progress_hooks=_media_progress_hooks(retry_reporter, HubKind.audio),
                    postprocessor_hooks=_media_postprocessor_hooks(retry_reporter, HubKind.audio),
                )
            reporter = retry_reporter
        if not options.quiet:
            _print_saved_result(
                "Audio saved",
                reporter,
                options.output_dir,
                archive_enabled=retry_options is None and options.archive,
                archive_file=options.archive_file,
            )
        return [Path(path) for path in reporter.saved_paths]
    except AtlasError as exc:
        _handle_error(exc, verbose=options.verbose)
        raise typer.Exit(1) from exc


def _run_file_download(
    settings: AtlasSettings,
    options: FileDownloadOptions,
    *,
    preview: dict[str, object] | None = None,
) -> list[Path]:
    try:
        engine = FileDownloadEngine()
        plan = engine.plan(options)
        if options.dry_run:
            result = engine.download(options)
            _print_backend_plan(result.ydl_opts or {}, json_output=options.json_output)
            return []
        if options.explain:
            _print_backend_plan(
                preview or {},
                json_output=options.json_output,
                explain=True,
            )
            return []
        if not options.quiet:
            _print_file_summary(options, plan.backend, plan.output, preview=preview)
        if options.quiet:
            result = DirectFileAdapter().run(options)
        else:
            progress_mode = _active_progress_mode(options)
            with FileProgressReporter(
                console,
                title=plan.output.name,
                mode=progress_mode,
                work_context=_file_work_context(options, backend=plan.backend, output=plan.output),
                alternate_screen=_alternate_screen_enabled(progress_mode),
            ) as reporter:
                result = DirectFileAdapter().run(options, progress_callback=reporter.handle_event)
        if not options.quiet:
            _print_backend_result("File saved", result.message, options.output_dir)
        return [plan.output]
    except AtlasError as exc:
        _handle_error(exc, verbose=options.verbose)
        raise typer.Exit(1) from exc


def _run_site_download(
    _settings: AtlasSettings,
    options: SiteDownloadOptions,
    *,
    preview: dict[str, object] | None = None,
) -> list[Path]:
    try:
        engine = SiteMirrorEngine()
        plan = engine.plan(options)
        if options.dry_run:
            result = engine.mirror(options)
            _print_backend_plan(result.ydl_opts or {}, json_output=options.json_output)
            return []
        if options.explain:
            _print_backend_plan(
                preview or {},
                json_output=options.json_output,
                explain=True,
            )
            return []
        if not options.quiet:
            _print_site_summary(options, plan.backend, preview=preview)
        try:
            if options.quiet:
                result = SiteMirrorAdapter().run(options)
            else:
                progress_mode = _active_progress_mode(options)
                with FileProgressReporter(
                    console,
                    title=plan.backend,
                    mode=progress_mode,
                    work_context=_site_work_context(
                        options,
                        backend=plan.backend,
                        output=options.output_dir,
                    ),
                    alternate_screen=_alternate_screen_enabled(progress_mode),
                ) as reporter:
                    result = SiteMirrorAdapter().run(
                        options,
                        progress_callback=reporter.handle_event,
                    )
        except AtlasError as exc:
            failure_result = DownloadResult(
                status=DownloadStatus.failed,
                url=options.url,
                message=str(exc),
                ydl_opts={
                    "backend": plan.backend,
                    "output": str(options.output_dir),
                    "warnings": plan.warnings,
                },
            )
            try:
                artifact_paths = _write_mirror_artifacts(
                    options,
                    failure_result,
                    backend=plan.backend,
                )
                if not options.quiet:
                    _print_artifact_panel(artifact_paths)
            except OSError as artifact_exc:
                logging.getLogger(__name__).warning(
                    "could not write mirror artifacts after failure: %s",
                    artifact_exc,
                )
            raise
        artifact_paths = _write_mirror_artifacts(options, result, backend=plan.backend)
        if not options.quiet:
            label = (
                "Directory mirror complete"
                if isinstance(options, DirectoryMirrorOptions)
                else "Site mirror complete"
            )
            _print_backend_result(label, result.message, options.output_dir)
            stats = (result.ydl_opts or {}).get("stats")
            _print_wget2_stats(stats if isinstance(stats, dict) else None)
            _print_artifact_panel(artifact_paths)
        return [plan.output]
    except AtlasError as exc:
        _handle_error(exc, verbose=options.verbose)
        raise typer.Exit(1) from exc


def _run_hub_get(
    settings: AtlasSettings,
    *,
    url: str,
    kind: HubKind,
    output_dir: Path | None,
    backend: str,
    audio: bool,
    checksum: str | None,
    video_codec: VideoCodecChoice,
    audio_codec: AudioCodec | None,
    audio_quality: int | None,
    dry_run: bool,
    adaptive: bool,
    max_concurrency: int | None,
    per_host_concurrency: int | None,
    politeness: AdaptivePoliteness,
    explain: bool,
    json_output: bool,
    quiet: bool,
    progress_mode: ProgressMode,
    verbose: bool,
) -> list[Path]:
    request = HubRequest(
        url=url,
        requested_kind=HubKind.audio if audio else kind,
        output_dir=output_dir or settings.output_dir,
        backend=backend,
        audio=audio,
        video_codec=video_codec,
        audio_codec=audio_codec,
        audio_quality=audio_quality,
        dry_run=dry_run,
        adaptive=adaptive,
        max_concurrency=max_concurrency,
        per_host_concurrency=per_host_concurrency,
        politeness=politeness,
        explain=explain,
        quiet=quiet,
        json_output=json_output,
        progress_mode=progress_mode,
        verbose=verbose,
    )
    route = EngineRouter(settings).route(request)
    execution_plan = DownloadOptimizer(settings).optimize(
        request,
        route,
        backend=backend,
        checksum=checksum,
    )
    if explain and execution_plan.route.kind in {HubKind.audio, HubKind.video}:
        if json_output:
            _print_json(plan_as_dict(execution_plan.preview))
        elif not quiet:
            _print_hub_plan(execution_plan)
        return []
    if dry_run and json_output:
        _print_json(plan_as_dict(execution_plan.preview))
        return []
    if not quiet and not json_output:
        _print_hub_plan(execution_plan)
    return _execute_hub_plan(settings, execution_plan)


def _print_hub_plan(plan: HubExecutionPlan) -> None:
    route = plan.route
    rows = [
        ("Detected", route.kind.value),
        ("Engine", route.engine.value),
        ("Reason", route.reason),
        ("Output", _styled_path(plan.preview.output or route.output_dir)),
    ]
    if plan.preview.session is not None:
        session = plan.preview.session
        rows.append(("Session", session.session_type))
        mode = session.scheduler_policy.get("mode")
        strategy = session.scheduler_policy.get("strategy")
        if mode or strategy:
            label = visual_join(str(value) for value in (mode, strategy) if value)
            rows.append(("Scheduler", label))
    for key, value in plan.preview.summary.items():
        if value is None:
            continue
        if key == "adaptive" and isinstance(value, dict):
            rows.append(("Adaptive", escape(str(value.get("strategy") or "enabled"))))
            rows.append(("Queue", str(value.get("queue_concurrency") or "-")))
            rows.append(("Segments", str(value.get("per_file_segments") or "-")))
            rows.append(("Per host", str(value.get("per_host_concurrency") or "-")))
            continue
        rows.append((str(key).replace("_", " ").title(), _plan_value(value)))
    if route.safety:
        rows.append(("Safety", "; ".join(route.safety)))
    _metadata_panel("Download Plan", rows)


def _plan_value(value: object) -> str:
    if isinstance(value, bool):
        return "enabled" if value else "disabled"
    return escape(str(value))


def _execute_hub_plan(settings: AtlasSettings, plan: HubExecutionPlan) -> list[Path]:
    options = plan.options
    if isinstance(options, AudioDownloadOptions):
        return _run_audio_download(settings, options)
    if isinstance(options, VideoDownloadOptions):
        return _run_video_download(settings, options)
    if isinstance(options, SiteDownloadOptions):
        if options.dry_run:
            _print_backend_plan(plan_as_dict(plan.preview), json_output=options.json_output)
            return []
        if options.explain:
            _print_backend_plan(
                plan_as_dict(plan.preview),
                json_output=options.json_output,
                explain=True,
            )
            return []
        return _run_site_download(settings, options, preview=plan_as_dict(plan.preview))
    if options.dry_run:
        _print_backend_plan(plan_as_dict(plan.preview), json_output=options.json_output)
        return []
    if options.explain:
        _print_backend_plan(
            plan_as_dict(plan.preview),
            json_output=options.json_output,
            explain=True,
        )
        return []
    return _run_file_download(settings, options, preview=plan_as_dict(plan.preview))


def _execute_options_or_exit(
    settings: AtlasSettings,
    options: (
        AudioDownloadOptions
        | VideoDownloadOptions
        | FileDownloadOptions
        | SiteDownloadOptions
        | DirectoryMirrorOptions
    ),
    kind: HubKind,
) -> list[Path]:
    try:
        return _execute_hub_plan(settings, _hub_plan_from_options(settings, options, kind))
    except AtlasError as exc:
        _handle_error(exc, verbose=options.verbose)
        raise typer.Exit(1) from exc


def _hub_plan_from_options(
    settings: AtlasSettings,
    options: (
        AudioDownloadOptions
        | VideoDownloadOptions
        | FileDownloadOptions
        | SiteDownloadOptions
        | DirectoryMirrorOptions
    ),
    kind: HubKind,
) -> HubExecutionPlan:
    backend = "auto"
    if isinstance(options, FileDownloadOptions | SiteDownloadOptions):
        backend = options.backend.value
    request = HubRequest(
        url=options.url,
        requested_kind=kind,
        output_dir=options.output_dir,
        backend=backend,
        audio=kind == HubKind.audio,
        dry_run=options.dry_run,
        adaptive=bool(getattr(options, "adaptive", False)),
        max_concurrency=getattr(options, "max_concurrency", None),
        per_host_concurrency=getattr(options, "per_host_concurrency", None),
        politeness=getattr(options, "politeness", AdaptivePoliteness.normal),
        explain=bool(getattr(options, "explain", False)),
        quiet=options.quiet,
        json_output=options.json_output,
        progress_mode=options.progress_mode,
        verbose=options.verbose,
    )
    route = EngineRouter(settings).route(request)
    if kind == HubKind.auto:
        return DownloadOptimizer(settings).optimize(request, route, backend=backend)
    return DownloadOptimizer(settings).optimize_options(route, options)


class _CliMenuActions:
    def __init__(self, settings: AtlasSettings) -> None:
        self._settings = settings

    def build_plan(self, options: MenuDownloadOptions, kind: HubKind) -> HubExecutionPlan:
        return _hub_plan_from_options(self._settings, options, kind)

    def print_plan(self, plan: HubExecutionPlan) -> None:
        _print_hub_plan(plan)

    def execute_plan(self, plan: HubExecutionPlan) -> list[Path]:
        try:
            return _execute_hub_plan(self._settings, plan)
        except typer.Exit as exc:
            cause = exc.__cause__
            detail = (
                str(cause)
                if isinstance(cause, AtlasError)
                else "The download command exited before it completed."
            )
            raise AtlasError(detail) from exc

    def run_info(self, url: str) -> None:
        info(url)

    def run_formats(self, url: str) -> None:
        formats(url)

    def probe_media(self, url: str, *, playlist: bool = False) -> MediaInfo:
        return _probe(_engine(self._settings)).probe(InfoOptions(url=url, playlist=playlist))

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
        batch(
            file,
            kind=kind,
            allow_sites=allow_sites,
            allow_dirs=allow_dirs,
            concurrency=concurrency,
            video_codec=video_codec,
            codec=audio_codec,
            audio_quality=audio_quality,
            dry_run=dry_run,
        )

    def resume_session(self, session: Path | None, *, dry_run: bool) -> None:
        _run_saved_batch_session(
            session,
            mode=BatchRetryMode.resume,
            output_dir=None,
            backend="auto",
            allow_sites=False,
            allow_dirs=False,
            concurrency=self._settings.batch_concurrency,
            adaptive=False,
            max_concurrency=None,
            per_host_concurrency=None,
            politeness=AdaptivePoliteness.normal,
            dry_run=dry_run,
            json_output=False,
            progress_mode=ProgressMode.auto,
            verbose=False,
        )

    def retry_failed_session(self, session: Path | None, *, dry_run: bool) -> None:
        _run_saved_batch_session(
            session,
            mode=BatchRetryMode.failed,
            output_dir=None,
            backend="auto",
            allow_sites=False,
            allow_dirs=False,
            concurrency=self._settings.batch_concurrency,
            adaptive=False,
            max_concurrency=None,
            per_host_concurrency=None,
            politeness=AdaptivePoliteness.normal,
            dry_run=dry_run,
            json_output=False,
            progress_mode=ProgressMode.auto,
            verbose=False,
        )

    def inspect_session(self, session: Path | None) -> None:
        inspect_session_command(session)

    def export_failed_session(self, session: Path | None, *, output: Path | None) -> None:
        export_failed_command(session, output=output)

    def scan_url(self, url: str) -> WorkItem:
        return scan_site(url)

    def run_backend_tool(self, tool: BackendTool, args: list[str], *, dry_run: bool) -> None:
        _run_backend_passthrough(
            tool,
            args,
            dry_run=dry_run,
            json_output=False,
            quiet=False,
            timeout=None,
            verbose=False,
        )

    def run_doctor(self) -> None:
        self._run_menu_command(doctor)

    def run_setup(self) -> None:
        self._run_menu_command(lambda: setup_command(no_install=True))

    def show_setup_plan(self) -> None:
        _print_setup_plan(build_setup_plan(self._settings, mode=SetupMode.full))

    def run_setup_install(self) -> None:
        self._run_menu_command(lambda: setup_command(full=True, install=True))

    def run_update(self) -> None:
        self._run_menu_command(lambda: update_command(dry_run=True))

    @staticmethod
    def _run_menu_command(operation: Callable[[], None]) -> None:
        try:
            operation()
        except typer.Exit:
            return

    def show_config(self) -> None:
        config_show()

    def show_config_path(self) -> None:
        config_path_command()

    def open_config_file(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        open_command = shutil.which("open")
        if open_command is None:
            raise AtlasError("Cannot open config file: macOS 'open' command was not found.")
        target = path if path.exists() else path.parent
        subprocess.run([open_command, str(target)], check=False)


def _adaptive_batch_plan_from_file(
    settings: AtlasSettings,
    *,
    file: Path,
    kind: BatchKind,
    output_dir: Path,
    backend: str,
    allow_sites: bool,
    allow_dirs: bool = False,
    controls: AdaptiveControls,
) -> AdaptiveDownloadPlan | None:
    entries, _skipped = load_batch_file(file)
    items: list[WorkItem] = []
    requested_kind = _batch_hub_kind(kind)
    for entry in entries:
        request = HubRequest(
            url=entry.url,
            requested_kind=requested_kind,
            output_dir=output_dir,
            backend=backend,
            audio=requested_kind == HubKind.audio,
        )
        route = EngineRouter(settings).route(request)
        if kind == BatchKind.auto and _is_batch_site_candidate(route):
            if allow_dirs:
                item = scan_site(entry.url, dry_run=controls.dry_run)
                item = item.model_copy(update={"kind": HubKind.dir})
                items.extend(plan_items_from_site_scan(item, kind=HubKind.dir))
            elif allow_sites:
                item = scan_site(entry.url, dry_run=controls.dry_run)
                items.extend(plan_items_from_site_scan(item, kind=HubKind.site))
            continue
        if kind == BatchKind.site:
            if not allow_sites:
                continue
            item = scan_site(entry.url, dry_run=controls.dry_run)
            items.extend(plan_items_from_site_scan(item, kind=HubKind.site))
        elif kind == BatchKind.dir:
            if not allow_dirs:
                continue
            item = scan_site(entry.url, dry_run=controls.dry_run)
            item = item.model_copy(update={"kind": HubKind.dir})
            items.extend(plan_items_from_site_scan(item, kind=HubKind.dir))
        elif route.kind == HubKind.file and not route.is_media_host:
            items.append(scan_direct_file(entry.url, dry_run=controls.dry_run))
        elif route.kind in {HubKind.video, HubKind.audio} or route.is_media_host:
            items.append(_work_item_from_route(route, dry_run=controls.dry_run))
    if not items:
        return None
    if any(item.kind == HubKind.dir for item in items):
        plan_kind = HubKind.dir
    elif any(item.kind == HubKind.site for item in items):
        plan_kind = HubKind.site
    else:
        plan_kind = HubKind.file
    return build_plan_for_items(
        items,
        controls=controls,
        kind=plan_kind,
        backend=backend,
    )


def _work_item_from_route(route: EngineRoute, *, dry_run: bool) -> WorkItem:
    parsed = urlparse(route.url)
    return WorkItem(
        url=route.url,
        host=parsed.hostname,
        kind=route.kind,
        size_class=FileSizeClass.unknown,
        bucket=WorkBucket.media,
        selected_backend=route.engine.value,
        priority=70,
        probed=False,
        error="dry run: media metadata probe skipped" if dry_run else None,
    )


def _batch_hub_kind(kind: BatchKind) -> HubKind:
    if kind == BatchKind.file:
        return HubKind.file
    if kind == BatchKind.audio:
        return HubKind.audio
    if kind == BatchKind.video:
        return HubKind.video
    if kind == BatchKind.site:
        return HubKind.site
    if kind == BatchKind.dir:
        return HubKind.dir
    return HubKind.auto


def _batch_file_filename_overrides(entries: Sequence[BatchEntry]) -> dict[int, str]:
    by_default_name: dict[str, list[BatchEntry]] = {}
    for entry in entries:
        by_default_name.setdefault(filename_from_url(entry.url), []).append(entry)
    reserved = {name for name, grouped in by_default_name.items() if len(grouped) == 1}
    overrides: dict[int, str] = {}
    used = set(reserved)
    for grouped in by_default_name.values():
        if len(grouped) <= 1:
            continue
        for entry in grouped:
            filename = _path_scoped_batch_filename(entry.url, line_no=entry.line_no, used=used)
            overrides[entry.line_no] = filename
            used.add(filename)
    return overrides


def _path_scoped_batch_filename(url: str, *, line_no: int, used: set[str]) -> str:
    parsed = urlparse(url)
    segments = [
        safe_filename(segment, default="")
        for segment in unquote(parsed.path).split("/")
        if segment.strip()
    ]
    segments = [segment for segment in segments if segment]
    if not segments:
        candidate = filename_from_url(url)
    elif len(segments) == 1:
        candidate = segments[0]
    else:
        candidate = "__".join(segments[-2:])
    if candidate not in used:
        return candidate
    path = Path(candidate)
    suffix = path.suffix
    stem = path.stem if suffix else candidate
    fallback = f"{stem}__line-{line_no}{suffix}"
    counter = 2
    while fallback in used:
        fallback = f"{stem}__line-{line_no}-{counter}{suffix}"
        counter += 1
    return fallback


def _apply_batch_filename_override(
    settings: AtlasSettings,
    plan: HubExecutionPlan,
    filename_overrides: Mapping[int, str] | None,
    entry: BatchEntry,
) -> HubExecutionPlan:
    if not filename_overrides or not isinstance(plan.options, FileDownloadOptions):
        return plan
    filename = filename_overrides.get(entry.line_no)
    if not filename:
        return plan
    options = plan.options.model_copy(update={"filename": filename})
    return DownloadOptimizer(settings).optimize_options(plan.route, options)


def _batch_hub_plan_from_url(
    settings: AtlasSettings,
    *,
    url: str,
    kind: BatchKind,
    output_dir: Path,
    backend: str,
    dry_run: bool,
    json_output: bool,
    verbose: bool,
    allow_sites: bool,
    allow_dirs: bool,
    video_codec: VideoCodecChoice = VideoCodecChoice.auto,
    audio_codec: AudioCodec | None = None,
    audio_quality: int | None = None,
    adaptive: bool = False,
    max_concurrency: int | None = None,
    per_host_concurrency: int | None = None,
    politeness: AdaptivePoliteness = AdaptivePoliteness.normal,
    explain: bool = False,
) -> HubExecutionPlan | DownloadResult:
    if kind == BatchKind.auto and is_explicit_playlist_url(url):
        message = (
            "Skipped explicit playlist URL in auto batch; use atlas playlist URL "
            "--type video|audio, or choose Download a playlist as batch in the menu."
        )
        route = EngineRouter(settings).route(
            HubRequest(
                url=url,
                requested_kind=HubKind.video,
                output_dir=output_dir,
                backend=backend,
                dry_run=dry_run,
                quiet=True,
                json_output=json_output,
                verbose=verbose,
            )
        )
        return DownloadResult(
            status=DownloadStatus.skipped,
            url=url,
            message=message,
            ydl_opts=_batch_skipped_plan(route, message),
        )
    requested_kind = _batch_hub_kind(kind)
    request = HubRequest(
        url=url,
        requested_kind=requested_kind,
        output_dir=output_dir,
        backend=backend,
        audio=requested_kind == HubKind.audio,
        video_codec=video_codec,
        audio_codec=audio_codec,
        audio_quality=audio_quality,
        dry_run=dry_run,
        adaptive=adaptive,
        max_concurrency=max_concurrency,
        per_host_concurrency=per_host_concurrency,
        politeness=politeness,
        explain=explain,
        quiet=True,
        json_output=json_output,
        verbose=verbose,
    )
    route = EngineRouter(settings).route(request)
    if kind == BatchKind.auto and _is_batch_site_candidate(route):
        if allow_dirs:
            dir_request = request.model_copy(update={"requested_kind": HubKind.dir})
            route = EngineRouter(settings).route(dir_request)
            request = dir_request
        elif allow_sites:
            site_request = request.model_copy(update={"requested_kind": HubKind.site})
            route = EngineRouter(settings).route(site_request)
            request = site_request
        else:
            message = (
                "Skipped possible website or directory mirror; "
                "pass --allow-sites or --allow-dirs to mirror recursively."
            )
            return DownloadResult(
                status=DownloadStatus.skipped,
                url=url,
                message=message,
                ydl_opts=_batch_skipped_plan(route, message),
            )
    elif kind == BatchKind.site:
        if not allow_sites:
            message = "Skipped website mirror; pass --allow-sites to mirror sites."
            return DownloadResult(
                status=DownloadStatus.skipped,
                url=url,
                message=message,
                ydl_opts=_batch_skipped_plan(route, message),
            )
    elif kind == BatchKind.dir and not allow_dirs:
        message = "Skipped open directory mirror; pass --allow-dirs to mirror open directories."
        return DownloadResult(
            status=DownloadStatus.skipped,
            url=url,
            message=message,
            ydl_opts=_batch_skipped_plan(route, message),
        )
    return DownloadOptimizer(settings).optimize(request, route, backend=backend)


def _run_batch_hub_plan(
    settings: AtlasSettings,
    plan: HubExecutionPlan,
    *,
    progress_hooks: list[ProgressHook] | None,
    postprocessor_hooks: list[ProgressHook] | None,
    progress_callback: Callable[[ProgressEvent], None] | None,
    process_control: ProcessControl | None = None,
) -> DownloadResult:
    options = plan.options
    plan_preview = plan_as_dict(plan.preview)
    if options.dry_run:
        return DownloadResult(
            status=DownloadStatus.dry_run,
            url=plan.route.url,
            message=_batch_plan_message(plan),
            ydl_opts=plan_preview,
        )
    if isinstance(options, AudioDownloadOptions):
        ensure_download_dependencies(
            settings,
            BatchKind.audio,
            SmartPlanner(settings).plan_audio(options),
        )
        result = _engine(settings).download_audio(
            options,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
        )
        return _download_result_with_batch_plan(result, plan_preview)
    if isinstance(options, VideoDownloadOptions):
        ensure_download_dependencies(
            settings,
            BatchKind.video,
            SmartPlanner(settings).plan_video(options),
        )
        result = _engine(settings).download_video(
            options,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
        )
        return _download_result_with_batch_plan(result, plan_preview)
    if isinstance(options, SiteDownloadOptions):
        result = SiteMirrorAdapter().run(
            options,
            progress_callback=progress_callback,
            control=process_control,
        )
        return _download_result_with_batch_plan(result, plan_preview)
    result = DirectFileAdapter().run(options, progress_callback=progress_callback)
    return _download_result_with_batch_plan(result, plan_preview)


def _download_result_with_batch_plan(
    result: DownloadResult,
    plan_preview: dict[str, object],
) -> DownloadResult:
    """Attach the routed plan to real batch results for final summaries."""

    payload = dict(plan_preview)
    if result.ydl_opts:
        payload["result"] = result.ydl_opts
    return result.model_copy(update={"ydl_opts": payload})


def _batch_progress_callback(
    reporter: BatchProgressReporter | None,
    entry: BatchEntry,
    *,
    adaptive_plan: AdaptiveDownloadPlan | None = None,
    adaptive_scheduler: AdaptiveScheduler | None = None,
    adaptive_items_by_url: Mapping[str, WorkItem] | None = None,
    tls_fallback_retry: bool = False,
) -> Callable[[ProgressEvent], None] | None:
    if reporter is None:
        return None

    def callback(event: ProgressEvent) -> None:
        if tls_fallback_retry and _event_is_tls_fallback_retry(event):
            event = _tls_fallback_retry_event(event)
        updates = _adaptive_progress_updates(
            entry=entry,
            event=event,
            adaptive_plan=adaptive_plan,
            adaptive_scheduler=adaptive_scheduler,
            adaptive_items_by_url=adaptive_items_by_url,
        )
        updates.update(
            {
                "line_no": entry.line_no,
                "url": entry.url,
                "item_id": str(entry.line_no),
            }
        )
        reporter.hook(
            event.model_copy(
                update=updates
            )
        )

    return callback


def _event_is_tls_fallback_retry(event: ProgressEvent) -> bool:
    return (
        event.status in {"error", "failed"}
        and is_tls_certificate_failure(event.message)
    )


def _tls_fallback_retry_event(event: ProgressEvent) -> ProgressEvent:
    return event.model_copy(
        update={
            "engine": EngineKind.curl,
            "status": "retrying",
            "phase": ProgressPhase.download,
            "error_code": None,
            "message": "aria2 TLS chain failed; trying verified curl fallback",
        }
    )


def _emit_batch_result_event(
    reporter: BatchProgressReporter | None,
    entry: BatchEntry,
    result: DownloadResult,
    plan: HubExecutionPlan | None = None,
    *,
    adaptive_plan: AdaptiveDownloadPlan | None = None,
    adaptive_scheduler: AdaptiveScheduler | None = None,
    adaptive_items_by_url: Mapping[str, WorkItem] | None = None,
) -> None:
    if reporter is None or not hasattr(reporter, "hook"):
        return
    phase = ProgressPhase.error if result.status == DownloadStatus.failed else ProgressPhase.done
    status = {
        DownloadStatus.success: "done",
        DownloadStatus.dry_run: "done",
        DownloadStatus.skipped: "skipped",
        DownloadStatus.canceled: "canceled",
        DownloadStatus.failed: "error",
    }[result.status]
    event = ProgressEvent(
        engine=_batch_result_event_engine(result, plan),
        status=status,
        phase=phase,
        kind=plan.route.kind if plan is not None else None,
        url=entry.url,
        title=entry.url,
        item_id=str(entry.line_no),
        line_no=entry.line_no,
        message=result.message,
    )
    updates = _adaptive_progress_updates(
        entry=entry,
        event=event,
        adaptive_plan=adaptive_plan,
        adaptive_scheduler=adaptive_scheduler,
        adaptive_items_by_url=adaptive_items_by_url,
    )
    reporter.hook(
        event.model_copy(update=updates) if updates else event
    )


def _batch_result_event_engine(
    result: DownloadResult,
    plan: HubExecutionPlan | None,
) -> EngineKind:
    backend = None
    if result.ydl_opts:
        result_payload = result.ydl_opts.get("result")
        if isinstance(result_payload, Mapping):
            backend = result_payload.get("backend")
        backend = backend or result.ydl_opts.get("backend")
    if backend:
        try:
            return EngineKind(str(backend))
        except ValueError:
            return EngineKind.unknown
    return plan.route.engine if plan is not None else EngineKind.unknown


def _batch_postprocessor_hooks(
    reporter: BatchProgressReporter | None,
    entry: BatchEntry,
    kind: HubKind,
) -> list[ProgressHook] | None:
    if reporter is None or not hasattr(reporter, "hook_for"):
        return None
    return [
        create_batch_postprocessor_hook(
            reporter,
            line_no=entry.line_no,
            url=entry.url,
            kind=kind,
        )
    ]


def _is_batch_site_candidate(route: EngineRoute) -> bool:
    if route.kind != HubKind.file or route.is_media_host:
        return False
    parsed = urlparse(route.url)
    path = parsed.path or "/"
    return path == "/" or path.endswith("/")


def _batch_skipped_plan(route: EngineRoute, message: str) -> dict[str, object]:
    return {
        "route": route.model_dump(mode="json"),
        "skipped_reason": message,
    }


def _batch_plan_message(plan: HubExecutionPlan) -> str:
    engine = plan.preview.summary.get("backend") or plan.route.engine.value
    return f"{plan.route.kind.value} via {engine}"


def _try_run_aria2_batch_queue(
    settings: AtlasSettings,
    *,
    file: Path,
    kind: BatchKind,
    output_dir: Path,
    backend: str,
    allow_sites: bool,
    allow_dirs: bool = False,
    resolved_concurrency: int,
    video_codec: VideoCodecChoice,
    audio_codec: AudioCodec | None,
    audio_quality: int | None,
    adaptive: bool,
    max_concurrency: int | None,
    per_host_concurrency: int | None,
    politeness: AdaptivePoliteness,
    verbose: bool,
    reporter: BatchProgressReporter | None,
    adaptive_plan: AdaptiveDownloadPlan | None = None,
    filename_overrides: Mapping[int, str] | None = None,
    adaptive_items_by_url: Mapping[str, WorkItem] | None = None,
) -> BatchSummary | None:
    entries, skipped = load_batch_file(file)
    if not entries:
        return BatchSummary(kind=kind, total=skipped, skipped=skipped)

    planned_items: list[tuple[BatchEntry, FileDownloadOptions, Path, HubExecutionPlan]] = []
    for entry in entries:
        try:
            planned = _batch_hub_plan_from_url(
                settings,
                url=entry.url,
                kind=kind,
                output_dir=output_dir,
                backend=backend,
                dry_run=False,
                json_output=False,
                verbose=verbose,
                allow_sites=allow_sites,
                allow_dirs=allow_dirs,
                video_codec=video_codec,
                audio_codec=audio_codec,
                audio_quality=audio_quality,
                adaptive=adaptive,
                max_concurrency=max_concurrency,
                per_host_concurrency=per_host_concurrency,
                politeness=politeness,
                explain=False,
            )
        except Exception:
            return None
        if isinstance(planned, DownloadResult):
            return None
        planned = _apply_batch_filename_override(settings, planned, filename_overrides, entry)
        if not isinstance(planned.options, FileDownloadOptions):
            return None
        if planned.options.backend != FileBackendChoice.aria2:
            return None
        if planned.preview.output is None:
            return None
        planned_items.append((entry, planned.options, planned.preview.output, planned))

    if _batch_should_skip_shared_aria2_for_verified_curl(planned_items):
        return None

    runtime_scheduler = (
        _adaptive_batch_runtime_scheduler(adaptive_plan)
        if adaptive and adaptive_plan is not None
        else None
    )
    queued = [
        Aria2RpcQueuedDownload(
            options=options,
            output=output,
            progress_callback=_batch_progress_callback(
                reporter,
                entry,
                adaptive_plan=adaptive_plan,
                adaptive_scheduler=runtime_scheduler,
                adaptive_items_by_url=adaptive_items_by_url,
                tls_fallback_retry=True,
            ),
        )
        for entry, options, output, _plan in planned_items
    ]
    try:
        first_options = queued[0].options
        session = Aria2RpcSession(
            max_concurrent_downloads=resolved_concurrency,
            input_file=first_options.input_file,
            save_session=first_options.save_session,
            save_session_interval=first_options.save_session_interval,
            server_stat_if=first_options.server_stat_if,
            server_stat_of=first_options.server_stat_of,
            server_stat_timeout=first_options.server_stat_timeout,
            uri_selector=first_options.uri_selector.value if first_options.uri_selector else None,
        )
        results = (
            session.download_many(queued, adaptive_scheduler=runtime_scheduler)
            if runtime_scheduler is not None
            else session.download_many(queued)
        )
    except Aria2RpcStartupError:
        return None

    if results and all(
        result.error and is_tls_certificate_failure(result.error)
        for result in results
    ):
        return None

    summary = BatchSummary(kind=kind, total=len(entries) + skipped, skipped=skipped)
    for (entry, _options, _output, plan), result in zip(planned_items, results, strict=True):
        plan_payload = plan_as_dict(plan.preview)
        plan_payload["queue"] = {
            "engine": EngineKind.aria2.value,
            "session": "shared",
            "max_concurrent_downloads": resolved_concurrency,
        }
        if result.error or result.result is None:
            summary.failed += 1
            summary.results.append(
                BatchItemResult(
                    entry=entry,
                    status=DownloadStatus.failed,
                    message=result.error or "aria2c download failed",
                    plan=plan_payload,
                )
            )
            continue
        summary.succeeded += 1
        summary.results.append(
            BatchItemResult(
                entry=entry,
                status=DownloadStatus.success,
                message=f"Saved to {result.result.output}",
                plan=plan_payload,
            )
        )
    return summary


def _batch_should_skip_shared_aria2_for_verified_curl(
    planned_items: list[tuple[BatchEntry, FileDownloadOptions, Path, HubExecutionPlan]],
) -> bool:
    return bool(planned_items) and all(
        options.probe is not None
        and is_tls_certificate_failure(options.probe.error)
        and can_attempt_verified_curl_fallback(options, output)
        for _entry, options, output, _plan in planned_items
    )


@contextmanager
def _artifact_write_lock(artifact_dir: Path) -> Iterator[None]:
    """Serialize artifact generation changes across local Atlas processes."""

    lock_path = artifact_dir / ".write.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            import fcntl
        except ImportError:
            yield
            return
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_private_artifact(path: Path, text: str) -> None:
    """Create one new artifact with private permissions and durable contents."""

    try:
        write_private_text(path, text)
    except OSError as exc:
        raise AtlasError(f"Could not create artifact {path}: {exc}") from exc


def _publish_latest_artifacts(
    artifact_dir: Path,
    files: Mapping[str, str],
) -> dict[str, Path]:
    """Publish a complete latest-session directory in one directory swap."""

    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AtlasError(f"Could not create Atlas artifact folder {artifact_dir}: {exc}") from exc
    if artifact_dir.is_symlink():
        raise AtlasError("Refusing to publish artifacts through a symbolic link.")
    latest_dir = artifact_dir / "latest"
    with _artifact_write_lock(artifact_dir):
        if latest_dir.is_symlink():
            raise AtlasError("Refusing to replace a symbolic-link latest session directory.")
        if latest_dir.exists() and not latest_dir.is_dir():
            raise AtlasError(f"Latest session path is not a directory: {latest_dir}")
        generation = uuid4().hex
        staging_dir = artifact_dir / f".latest-staging-{generation}"
        previous_dir = artifact_dir / f".latest-previous-{generation}"
        try:
            staging_dir.mkdir(mode=0o700)
            for name, text in files.items():
                _write_private_artifact(staging_dir / name, text)
            moved_previous = False
            if latest_dir.exists():
                os.replace(latest_dir, previous_dir)
                moved_previous = True
            try:
                os.replace(staging_dir, latest_dir)
            except OSError:
                if moved_previous and previous_dir.exists():
                    os.replace(previous_dir, latest_dir)
                raise
            if moved_previous:
                shutil.rmtree(previous_dir, ignore_errors=True)
        except OSError as exc:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise AtlasError(f"Could not publish latest Atlas artifacts: {exc}") from exc
        except AtlasError:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
    return {name: latest_dir / name for name in files}


def _write_batch_artifacts(
    summary: BatchSummary,
    *,
    output_dir: Path,
    adaptive_plan: AdaptiveDownloadPlan | None,
    source: str = "batch",
) -> dict[str, Path]:
    artifact_dir = output_dir.expanduser() / ".atlas"
    latest_dir = artifact_dir / "latest"
    stamp = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid4().hex[:8]}"
    summary_path = artifact_dir / f"batch-summary-{stamp}.json"
    manifest_path = artifact_dir / f"batch-manifest-{stamp}.json"
    retry_path = artifact_dir / f"batch-retry-{stamp}.txt"
    latest_summary_path = latest_dir / "summary.json"
    latest_manifest_path = latest_dir / "manifest.json"
    failed_path = latest_dir / "failed.txt"
    skipped_path = latest_dir / "skipped.txt"
    canceled_path = latest_dir / "canceled.txt"
    retry_manifest_path = latest_dir / "retry.atlas.json"

    summary_payload = summary.model_dump(mode="json")
    summary_text = json.dumps(summary_payload, indent=2, sort_keys=True) + "\n"
    failed_urls = [
        result.entry.url
        for result in summary.results
        if result.status == DownloadStatus.failed
        if result.entry.url.strip()
    ]
    skipped_urls = [
        result.entry.url
        for result in summary.results
        if result.status == DownloadStatus.skipped
        if result.entry.url.strip()
    ]
    canceled_urls = [
        result.entry.url
        for result in summary.results
        if result.status == DownloadStatus.canceled
        if result.entry.url.strip()
    ]
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "kind": summary.kind.value,
        "total": summary.total,
        "succeeded": summary.succeeded,
        "failed": summary.failed,
        "skipped": summary.skipped,
        "canceled": summary.canceled,
        "smart_session": batch_session(
            source=source,
            kind=summary.kind,
            output_dir=output_dir,
            adaptive_plan=adaptive_plan,
            total=summary.total,
            summary=summary,
        ).model_dump(mode="json"),
        "adaptive_plan": adaptive_plan.model_dump(mode="json") if adaptive_plan else None,
        "items": [
            {
                "line_no": result.entry.line_no,
                "url": result.entry.url,
                "status": result.status.value,
                "kind": _plain_batch_result_plan_value(result.plan, "kind"),
                "engine": _plain_batch_result_plan_value(result.plan, "engine"),
                "message": result.message,
                "backend_args": _batch_result_backend_args(result.plan),
                "backend_command": _batch_result_backend_command(result.plan),
            }
            for result in summary.results
        ],
    }
    retry_manifest = _batch_retry_manifest(
        summary,
        manifest_path=latest_manifest_path,
        summary_path=latest_summary_path,
        failed_urls=failed_urls,
        skipped_urls=skipped_urls,
        canceled_urls=canceled_urls,
    )
    manifest["artifacts"] = {
        "summary": str(latest_summary_path),
        "manifest": str(latest_manifest_path),
        "failed": str(failed_path),
        "skipped": str(skipped_path),
        "canceled": str(canceled_path),
        "retry": str(retry_manifest_path),
    }
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    retry_manifest_text = json.dumps(retry_manifest, indent=2, sort_keys=True) + "\n"
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AtlasError(f"Could not create Atlas artifact folder {artifact_dir}: {exc}") from exc
    if artifact_dir.is_symlink():
        raise AtlasError("Refusing to write artifacts through a symbolic link.")
    _write_private_artifact(summary_path, summary_text)
    _write_private_artifact(manifest_path, manifest_text)
    if failed_urls:
        _write_private_artifact(retry_path, "\n".join(failed_urls) + "\n")
    latest_paths = _publish_latest_artifacts(
        artifact_dir,
        {
            "summary.json": summary_text,
            "manifest.json": manifest_text,
            "failed.txt": ("\n".join(failed_urls) + "\n") if failed_urls else "",
            "skipped.txt": ("\n".join(skipped_urls) + "\n") if skipped_urls else "",
            "canceled.txt": ("\n".join(canceled_urls) + "\n") if canceled_urls else "",
            "retry.atlas.json": retry_manifest_text,
        },
    )
    paths = {
        "summary": summary_path,
        "manifest": manifest_path,
        "latest_summary": latest_paths["summary.json"],
        "latest_manifest": latest_paths["manifest.json"],
        "failed": latest_paths["failed.txt"],
        "skipped": latest_paths["skipped.txt"],
        "canceled": latest_paths["canceled.txt"],
        "retry_manifest": latest_paths["retry.atlas.json"],
    }
    if failed_urls:
        paths["retry"] = retry_path
    return paths


def _batch_retry_manifest(
    summary: BatchSummary,
    *,
    manifest_path: Path,
    summary_path: Path,
    failed_urls: list[str],
    skipped_urls: list[str],
    canceled_urls: list[str],
) -> dict[str, object]:
    checksum_failure_urls = [
        result.entry.url
        for result in summary.results
        if result.status == DownloadStatus.failed
        if result.entry.url.strip()
        if "checksum" in (result.message or "").lower()
    ]
    skipped_unknown_urls = [
        result.entry.url
        for result in summary.results
        if result.status == DownloadStatus.skipped
        if result.entry.url.strip()
        if _batch_result_is_unknown(result)
    ]
    return {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "kind": summary.kind.value,
        "manifest_path": str(manifest_path),
        "summary_path": str(summary_path),
        "retry_failed_only": failed_urls,
        "retry_checksum_failures_only": checksum_failure_urls,
        "retry_skipped_unknowns_only": skipped_unknown_urls,
        "retry_canceled_only": canceled_urls,
        "export_failed_urls": str(manifest_path.with_name("failed.txt")),
        "save_manifest": str(manifest_path),
        "load_manifest": str(manifest_path),
        "resume_previous_session": str(manifest_path),
        "skipped_urls": skipped_urls,
        "canceled_urls": canceled_urls,
    }


def _batch_result_is_unknown(result: BatchItemResult) -> bool:
    message = (result.message or "").lower()
    if "unknown" in message:
        return True
    if not result.plan:
        return True
    route = result.plan.get("route")
    if not isinstance(route, dict):
        return True
    return str(route.get("kind") or "").lower() in {"", "-", "unknown"}


def _batch_result_backend_args(plan: dict[str, object] | None) -> list[str]:
    if not isinstance(plan, dict):
        return []
    args = plan.get("args")
    if not isinstance(args, list):
        return []
    return [str(arg) for arg in args if str(arg).strip()]


def _batch_result_backend_command(plan: dict[str, object] | None) -> str | None:
    args = _batch_result_backend_args(plan)
    return shlex.join(args) if args else None


def _resolve_batch_session_path(session: Path | None, *, output_dir: Path) -> Path:
    if session is None:
        path = output_dir.expanduser() / ".atlas" / "latest" / "retry.atlas.json"
        if path.is_symlink():
            raise AtlasError("Refusing to load a saved session through a symbolic link.")
        if path.is_file():
            return path
        raise AtlasError(f"No saved session found at {path}")

    path = session.expanduser()
    if path.is_symlink():
        raise AtlasError("Refusing to load a saved session through a symbolic link.")
    if path.is_file():
        return path
    if path.is_dir():
        candidates = [
            path / "retry.atlas.json",
            path / "manifest.json",
            path / "latest" / "retry.atlas.json",
            path / ".atlas" / "latest" / "retry.atlas.json",
        ]
        for candidate in candidates:
            if candidate.is_symlink():
                raise AtlasError("Refusing to load a saved session through a symbolic link.")
            if candidate.is_file():
                return candidate
    raise AtlasError(f"No retry.atlas.json or manifest.json found at {path}")


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AtlasError(f"Could not read saved session {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AtlasError(f"Saved session is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise AtlasError(f"Saved session must be a JSON object: {path}")
    return raw


def _batch_session_payloads(path: Path) -> tuple[dict[str, object], dict[str, object] | None]:
    payload = _load_json_object(path)
    manifest_payload: dict[str, object] | None = None
    manifest_path = payload.get("manifest_path")
    if isinstance(manifest_path, str):
        candidate = Path(manifest_path).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        trust_root = _session_artifact_root(path)
        if not _path_is_within(candidate, trust_root):
            local_candidate = path.with_name(candidate.name)
            if local_candidate.is_file() and not local_candidate.is_symlink():
                candidate = local_candidate
            else:
                raise AtlasError(
                    "Saved session links to a manifest outside its trusted session folder."
                )
        if candidate.is_symlink():
            raise AtlasError("Refusing to load a linked manifest through a symbolic link.")
        if candidate.is_file() and not _paths_alias(candidate, path):
            loaded = _load_json_object(candidate)
            if "items" in loaded:
                manifest_payload = loaded
    if "items" in payload:
        manifest_payload = payload
    return payload, manifest_payload


def _session_artifact_root(path: Path) -> Path:
    expanded = path.expanduser().absolute()
    return next(
        (parent for parent in expanded.parents if parent.name == ".atlas"),
        expanded.parent,
    )


def _session_output_root(path: Path) -> Path | None:
    artifact_root = _session_artifact_root(path)
    if artifact_root.name != ".atlas":
        return None
    return artifact_root.parent


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.expanduser().resolve(strict=False).relative_to(
            root.expanduser().resolve(strict=False)
        )
    except (OSError, ValueError):
        return False
    return True


def _batch_session_kind(
    payload: dict[str, object],
    manifest: dict[str, object] | None,
) -> BatchKind:
    value = payload.get("kind") or (manifest or {}).get("kind") or BatchKind.auto.value
    try:
        return BatchKind(str(value))
    except ValueError:
        return BatchKind.auto


def _batch_session_output_dir(
    payload: dict[str, object],
    manifest: dict[str, object] | None,
    *,
    session_path: Path,
    fallback: Path,
) -> Path:
    trusted_output = _session_output_root(session_path)
    for candidate_payload in (manifest, payload):
        if not candidate_payload:
            continue
        smart_session = candidate_payload.get("smart_session")
        if not isinstance(smart_session, dict):
            continue
        customization = smart_session.get("customization")
        if not isinstance(customization, dict):
            continue
        output_dir = customization.get("output_dir")
        if isinstance(output_dir, str) and output_dir.strip():
            candidate = Path(output_dir).expanduser()
            if trusted_output is not None and _path_is_within(candidate, trusted_output):
                return candidate
    if trusted_output is not None:
        return trusted_output
    return fallback.expanduser()


def _retry_mode_from_flags(
    *,
    failed_only: bool,
    checksum_failures_only: bool,
    skipped_unknowns_only: bool,
    canceled_only: bool = False,
    resume_mode: bool = False,
) -> str:
    selected = [
        failed_only,
        checksum_failures_only,
        skipped_unknowns_only,
        canceled_only,
    ]
    if sum(1 for value in selected if value) > 1:
        raise AtlasError("Choose only one retry selector.")
    if resume_mode:
        return BatchRetryMode.resume
    if checksum_failures_only:
        return BatchRetryMode.checksum
    if skipped_unknowns_only:
        return BatchRetryMode.skipped_unknown
    if canceled_only:
        return BatchRetryMode.canceled
    return BatchRetryMode.failed


def _batch_session_urls_for_mode(
    payload: dict[str, object],
    manifest: dict[str, object] | None,
    *,
    mode: str,
) -> list[str]:
    if mode == BatchRetryMode.failed:
        urls = _payload_url_list(payload, "retry_failed_only")
        if urls:
            return urls
    elif mode == BatchRetryMode.checksum:
        urls = _payload_url_list(payload, "retry_checksum_failures_only")
        if urls:
            return urls
    elif mode == BatchRetryMode.skipped_unknown:
        urls = _payload_url_list(payload, "retry_skipped_unknowns_only")
        if urls:
            return urls
    elif mode == BatchRetryMode.canceled:
        urls = _payload_url_list(payload, "retry_canceled_only")
        if urls:
            return urls
    elif mode == BatchRetryMode.resume:
        urls = [
            *_payload_url_list(payload, "retry_failed_only"),
            *_payload_url_list(payload, "retry_skipped_unknowns_only"),
            *_payload_url_list(payload, "retry_canceled_only"),
        ]
        if urls:
            return _dedupe_urls(urls)

    if not manifest:
        return []
    return _manifest_urls_for_mode(manifest, mode=mode)


def _payload_url_list(payload: dict[str, object], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _manifest_urls_for_mode(payload: dict[str, object], *, mode: str) -> list[str]:
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    urls: list[str] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        status = str(raw_item.get("status") or "")
        message = str(raw_item.get("message") or "").lower()
        kind = str(raw_item.get("kind") or "").lower()
        url = str(raw_item.get("url") or "").strip()
        if not url:
            continue
        failed = status == DownloadStatus.failed.value
        canceled = status == DownloadStatus.canceled.value
        skipped_unknown = (
            status == DownloadStatus.skipped.value
            and ("unknown" in message or kind in {"", "-", "unknown"})
        )
        selected = (
            (mode == BatchRetryMode.failed and failed)
            or (mode == BatchRetryMode.checksum and failed and "checksum" in message)
            or (mode == BatchRetryMode.skipped_unknown and skipped_unknown)
            or (mode == BatchRetryMode.canceled and canceled)
            or (mode == BatchRetryMode.resume and (failed or skipped_unknown or canceled))
        )
        if selected:
            urls.append(url)
    return _dedupe_urls(urls)


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _write_retry_batch_file(
    urls: list[str],
    *,
    output_dir: Path,
    mode: str,
) -> Path:
    retry_dir = output_dir.expanduser() / ".atlas" / "retry"
    try:
        ensure_private_directory(retry_dir)
    except OSError as exc:
        raise AtlasError(f"Could not create private retry folder: {exc}") from exc
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = retry_dir / f"{mode}-{stamp}-{uuid4().hex}.txt"
    _write_private_artifact(path, "\n".join(urls) + "\n")
    return path


@contextmanager
def _retry_batch_source(
    urls: list[str],
    *,
    output_dir: Path,
    mode: str,
    dry_run: bool,
) -> Iterator[Path]:
    """Yield a retry input file without leaving artifacts for preview-only runs."""

    if not dry_run:
        yield _write_retry_batch_file(urls, output_dir=output_dir, mode=mode)
        return
    with tempfile.TemporaryDirectory(prefix="atlas-retry-preview-") as temporary:
        path = Path(temporary) / "urls.txt"
        try:
            write_private_text(path, "\n".join(urls) + "\n")
        except OSError as exc:
            raise AtlasError(f"Could not create retry preview: {exc}") from exc
        yield path


def _print_no_retry_urls(*, mode: str, json_output: bool) -> None:
    message = f"No URLs found for retry mode: {mode}"
    if json_output:
        console.print_json(json.dumps({"status": "empty", "mode": mode, "urls": []}))
        return
    console.print(f"[{ATLAS_WARNING_STYLE}]{escape(message)}[/{ATLAS_WARNING_STYLE}]")


def _inspect_saved_session(
    session_path: Path,
    payload: dict[str, object],
    manifest: dict[str, object] | None,
    *,
    output_dir: Path,
    item_line: int | None,
    limit: int,
    filter_text: str | None,
    status_filter: SessionStatusFilter,
    kind_filter: str | None,
    panel: SessionPanelChoice,
) -> dict[str, object]:
    summary = _session_summary_payload(payload, manifest, session_path=session_path)
    smart_session = _session_smart_payload(payload, manifest)
    scheduler = _dict_value(smart_session, "scheduler_policy")
    artifacts = _session_artifacts(payload, manifest, session_path=session_path)
    items = _session_items(manifest)
    filtered_items = _filter_session_items(
        items,
        filter_text=filter_text,
        status_filter=status_filter,
        kind_filter=kind_filter,
    )
    failed_items = [
        item for item in items if item.get("status") == DownloadStatus.failed.value
    ]
    filtered_failed_items = [
        item for item in filtered_items if item.get("status") == DownloadStatus.failed.value
    ]
    item_detail = _session_item_detail(items, item_line) if item_line is not None else None
    retry_failed = _batch_session_urls_for_mode(payload, manifest, mode=BatchRetryMode.failed)
    retry_checksum = _batch_session_urls_for_mode(payload, manifest, mode=BatchRetryMode.checksum)
    retry_skipped = _batch_session_urls_for_mode(
        payload,
        manifest,
        mode=BatchRetryMode.skipped_unknown,
    )
    retry_canceled = _batch_session_urls_for_mode(payload, manifest, mode=BatchRetryMode.canceled)
    retry_resume = _batch_session_urls_for_mode(payload, manifest, mode=BatchRetryMode.resume)
    counts = _session_counts(payload, manifest, summary)
    session_type = str(
        smart_session.get("session_type")
        or ((manifest or {}).get("kind"))
        or "-"
    )
    source = str(
        smart_session.get("source")
        or ((manifest or {}).get("source"))
        or "-"
    )
    commands = _session_operator_commands(session_path)
    backend_commands = _session_backend_commands(filtered_items, limit=limit)
    return {
        "status": "ok",
        "session": str(session_path),
        "kind": _batch_session_kind(payload, manifest).value,
        "session_type": session_type,
        "source": source,
        "created_at": str(
            (manifest or {}).get("created_at")
            or payload.get("created_at")
            or summary.get("created_at")
            or "-"
        ),
        "output_dir": str(output_dir),
        "counts": counts,
        "retry": {
            "failed": {"count": len(retry_failed), "urls": retry_failed},
            "checksum": {"count": len(retry_checksum), "urls": retry_checksum},
            "skipped_unknown": {"count": len(retry_skipped), "urls": retry_skipped},
            "canceled": {"count": len(retry_canceled), "urls": retry_canceled},
            "resume": {"count": len(retry_resume), "urls": retry_resume},
        },
        "commands": commands,
        "backend_commands": {
            "total": len(_session_backend_commands(filtered_items, limit=None)),
            "sample": backend_commands,
            "limit": limit,
        },
        "scheduler": scheduler,
        "artifacts": artifacts,
        "filters": {
            "query": filter_text.strip() if filter_text else None,
            "status": status_filter.value,
            "kind": kind_filter.strip() if kind_filter else None,
            "matched": len(filtered_items),
            "total": len(items),
        },
        "items": {
            "total": len(items),
            "matched": len(filtered_items),
            "sample": filtered_items[:limit],
            "limit": limit,
        },
        "panel": _session_panel_payload(
            panel,
            filtered_items=filtered_items,
            scheduler=scheduler,
            counts=counts,
            limit=limit,
        ),
        "errors": {
            "total": len(failed_items),
            "matched": len(filtered_failed_items),
            "sample": filtered_failed_items[:limit],
            "limit": limit,
        },
        "item": item_detail,
    }


def _session_operator_commands(session_path: Path) -> dict[str, str]:
    session_arg = str(session_path)
    return {
        SessionCommandChoice.retry.value: shlex.join(["atlas", "retry", session_arg]),
        SessionCommandChoice.resume.value: shlex.join(["atlas", "resume", session_arg]),
        SessionCommandChoice.export_failed.value: shlex.join(
            ["atlas", "export-failed", session_arg]
        ),
        SessionCommandChoice.inspect.value: shlex.join(["atlas", "inspect-session", session_arg]),
    }


def _session_backend_commands(
    items: list[dict[str, object]],
    *,
    limit: int | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in items:
        command = str(item.get("backend_command") or "").strip()
        if not command:
            continue
        rows.append(
            {
                "line_no": item.get("line_no"),
                "url": item.get("url"),
                "engine": item.get("engine"),
                "command": command,
                "args": _string_list(item.get("backend_args")),
            }
        )
        if limit is not None and len(rows) >= limit:
            break
    return rows


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _session_command_for_choice(
    report: dict[str, object],
    choice: SessionCommandChoice,
) -> str | None:
    if choice == SessionCommandChoice.none:
        return None
    if choice == SessionCommandChoice.backend:
        backend_commands = _dict_value(report, "backend_commands")
        sample = backend_commands.get("sample")
        if not isinstance(sample, list) or not sample:
            return None
        first = sample[0]
        if not isinstance(first, dict):
            return None
        command = first.get("command")
        return str(command) if command else None
    commands = _dict_value(report, "commands")
    command = commands.get(choice.value)
    return str(command) if command else None


def _session_summary_payload(
    payload: dict[str, object],
    manifest: dict[str, object] | None,
    *,
    session_path: Path,
) -> dict[str, object]:
    for value in (
        payload.get("summary_path"),
        _dict_value(manifest, "artifacts").get("summary") if manifest else None,
    ):
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = session_path.parent / candidate
        if candidate.exists():
            return _load_json_object(candidate)
    return {}


def _session_smart_payload(
    payload: dict[str, object],
    manifest: dict[str, object] | None,
) -> dict[str, object]:
    for candidate in (manifest, payload):
        smart_session = _dict_value(candidate, "smart_session")
        if smart_session:
            return smart_session
    return {}


def _session_artifacts(
    payload: dict[str, object],
    manifest: dict[str, object] | None,
    *,
    session_path: Path,
) -> dict[str, str]:
    artifacts = {key: str(value) for key, value in _dict_value(manifest, "artifacts").items()}
    artifacts.setdefault("session", str(session_path))
    for key in ("manifest_path", "summary_path", "export_failed_urls"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            artifacts.setdefault(key, value)
    return artifacts


def _session_items(manifest: dict[str, object] | None) -> list[dict[str, object]]:
    raw_items = (manifest or {}).get("items")
    if not isinstance(raw_items, list):
        return []
    return [item for item in raw_items if isinstance(item, dict)]


def _filter_session_items(
    items: list[dict[str, object]],
    *,
    filter_text: str | None,
    status_filter: SessionStatusFilter,
    kind_filter: str | None,
) -> list[dict[str, object]]:
    query = filter_text.strip().lower() if filter_text else ""
    wanted_status = status_filter.value if status_filter != SessionStatusFilter.all else ""
    wanted_kind = kind_filter.strip().lower() if kind_filter else ""

    def item_matches(item: dict[str, object]) -> bool:
        if wanted_status and str(item.get("status") or "").lower() != wanted_status:
            return False
        if wanted_kind and str(item.get("kind") or "").lower() != wanted_kind:
            return False
        if not query:
            return True
        searchable = " ".join(
            str(item.get(key) or "")
            for key in ("line_no", "url", "kind", "engine", "status", "message")
        ).lower()
        return query in searchable

    return [item for item in items if item_matches(item)]


def _session_panel_payload(
    panel: SessionPanelChoice,
    *,
    filtered_items: list[dict[str, object]],
    scheduler: dict[str, object],
    counts: dict[str, int],
    limit: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "selected": panel.value,
        "limit": limit,
    }
    if panel == SessionPanelChoice.scheduler:
        payload["fields"] = scheduler
        return payload
    if panel == SessionPanelChoice.summary:
        payload["counts"] = counts
        return payload
    if panel == SessionPanelChoice.logs:
        path = log_dir() / "atlas.log"
        payload["path"] = str(path)
        payload["available"] = path.exists()
        return payload
    if panel == SessionPanelChoice.overview:
        payload["total"] = len(filtered_items)
        payload["sample"] = filtered_items[:limit]
        return payload

    panel_items = [
        item for item in filtered_items if _item_in_session_panel(item, panel)
    ]
    payload["total"] = len(panel_items)
    payload["sample"] = panel_items[:limit]
    return payload


def _item_in_session_panel(item: dict[str, object], panel: SessionPanelChoice) -> bool:
    status = str(item.get("status") or "").lower()
    if panel == SessionPanelChoice.queue:
        return status in {"planned", "queued", "probing", "scanning", "resolving", "waiting"}
    if panel == SessionPanelChoice.active:
        return status not in {
            "",
            "success",
            "failed",
            "skipped",
            "canceled",
            "dry-run",
            "done",
            "error",
            "planned",
            "queued",
            "waiting",
        }
    if panel == SessionPanelChoice.completed:
        return status in {"success", "dry-run", "done"}
    if panel == SessionPanelChoice.failed:
        return status in {"failed", "error"}
    if panel == SessionPanelChoice.canceled:
        return status == "canceled"
    return False


def _session_item_detail(
    items: list[dict[str, object]],
    line_no: int,
) -> dict[str, object]:
    for item in items:
        if item.get("line_no") == line_no:
            return item
    raise AtlasError(f"No item with line {line_no} found in saved session.")


def _session_counts(
    payload: dict[str, object],
    manifest: dict[str, object] | None,
    summary: dict[str, object],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for key in ("total", "succeeded", "failed", "skipped", "canceled"):
        counts[key] = _int_from_payload(summary, key)
        if counts[key] == 0:
            counts[key] = _int_from_payload(manifest, key)
        if counts[key] == 0:
            counts[key] = _int_from_payload(payload, key)
    return counts


def _int_from_payload(payload: dict[str, object] | None, key: str) -> int:
    if not payload:
        return 0
    value = payload.get(key)
    return int(value) if isinstance(value, int) and value >= 0 else 0


def _dict_value(
    payload: dict[str, object] | None,
    key: str,
) -> dict[str, object]:
    value = (payload or {}).get(key)
    return value if isinstance(value, dict) else {}


def _print_saved_session_inspection(
    report: dict[str, object],
    *,
    preview: SessionPreviewChoice,
    panel: SessionPanelChoice,
    payload: dict[str, object],
    manifest: dict[str, object] | None,
) -> None:
    view = SmartSessionView(title="atlas")
    counts = _dict_value(report, "counts")
    scheduler = _dict_value(report, "scheduler")
    artifacts = _dict_value(report, "artifacts")
    commands = _dict_value(report, "commands")
    session_path = str(report.get("session") or "-")
    header_fields = [
        ViewField("Session", session_path, "path"),
        ViewField("Type", str(report.get("session_type") or "-"), "active"),
        ViewField("Kind", str(report.get("kind") or "-")),
        ViewField("Source", str(report.get("source") or "-"), "path"),
        ViewField("Output", str(report.get("output_dir") or "-"), "path"),
    ]
    filter_label = _session_filter_label(report)
    if filter_label:
        header_fields.append(ViewField("Filter", filter_label, "warning"))
    dashboard = view.progress_dashboard(
        heading="Saved Session",
        fields=tuple(header_fields),
        metrics=_session_metrics(counts),
        active_rows=_session_active_rows(report),
        scheduler=_session_scheduler_fields(scheduler),
        failures=_session_failure_rows(report),
    )
    console.print(dashboard)
    if panel != SessionPanelChoice.overview:
        console.print(
            view.panel_tabs(
                active=panel.value,
                labels=[choice.value for choice in SessionPanelChoice],
            )
        )
        _print_session_focus_panel(view, panel, report)
    console.print(
        view.customization_overlay(
            title="Operator Actions",
            description="Saved sessions can be retried, resumed, exported, or inspected again.",
            options=(
                ViewField("Retry failed", str(commands.get("retry") or "-")),
                ViewField("Resume", str(commands.get("resume") or "-")),
                ViewField("Export failed", str(commands.get("export-failed") or "-")),
                ViewField(
                    "Inspect item",
                    f"{commands.get('inspect') or 'atlas inspect-session SESSION'} --item LINE",
                ),
            ),
        )
    )
    if artifacts:
        console.print(
            view.customization_overlay(
                title="Artifacts",
                description="Stable files for automation and follow-up operations.",
                options=tuple(
                    ViewField(label, value, "path")
                    for label, value in sorted((str(k), str(v)) for k, v in artifacts.items())
                ),
            )
        )
    item = _dict_value(report, "item")
    if item:
        console.print(
            view.preview_panel(
                title=f"Item {item.get('line_no', '-')}",
                content=json.dumps(item, indent=2, sort_keys=True),
                syntax="json",
            )
        )
    preview_content = _session_preview_content(
        preview,
        report=report,
        payload=payload,
        manifest=manifest,
    )
    if preview_content is not None:
        title, content, syntax = preview_content
        console.print(view.preview_panel(title=title, content=content, syntax=syntax))


def _print_session_focus_panel(
    view: SmartSessionView,
    panel: SessionPanelChoice,
    report: dict[str, object],
) -> None:
    panel_payload = _dict_value(report, "panel")
    if panel in {
        SessionPanelChoice.queue,
        SessionPanelChoice.active,
        SessionPanelChoice.completed,
        SessionPanelChoice.canceled,
        SessionPanelChoice.failed,
    }:
        item_rows = _session_rows_from_items(panel_payload.get("sample"))
        console.print(
            view.state_panel(
                title=f"{panel.value.title()} Items",
                rows=item_rows,
                empty=f"No {panel.value} items in the current view.",
            )
        )
        return
    if panel == SessionPanelChoice.scheduler:
        scheduler = _dict_value(report, "scheduler")
        scheduler_rows = _session_scheduler_fields(scheduler)
        console.print(
            view.scheduler_panel(
                scheduler_rows or (ViewField("Status", "No scheduler data", "muted"),)
            )
        )
        return
    if panel == SessionPanelChoice.logs:
        path = log_dir() / "atlas.log"
        console.print(
            view.preview_panel(
                title="Atlas Log",
                content=_read_text_preview(path, missing=f"No log file found at {path}"),
                syntax="text",
            )
        )
        return
    counts = _dict_value(report, "counts")
    retry = _dict_value(report, "retry")
    console.print(
        view.final_summary(
            heading="Session Summary",
            fields=(
                ViewField("Total", str(counts.get("total", 0))),
                ViewField("Succeeded", str(counts.get("succeeded", 0)), "success"),
                ViewField("Failed", str(counts.get("failed", 0)), "error"),
                ViewField("Skipped", str(counts.get("skipped", 0)), "warning"),
                ViewField("Canceled", str(counts.get("canceled", 0)), "warning"),
                ViewField(
                    "Retryable",
                    str(
                        _safe_int(_dict_value(retry, "failed").get("count"))
                        + _safe_int(_dict_value(retry, "canceled").get("count"))
                    ),
                    "active",
                ),
            ),
            actions=("Retry failed", "Export failed", "Open output"),
        )
    )


def _session_filter_label(report: dict[str, object]) -> str:
    filters = _dict_value(report, "filters")
    status = str(filters.get("status") or SessionStatusFilter.all.value)
    query = filters.get("query")
    kind = filters.get("kind")
    parts: list[str] = []
    if status != SessionStatusFilter.all.value:
        parts.append(f"status={status}")
    if kind:
        parts.append(f"kind={kind}")
    if query:
        parts.append(f"query={query}")
    if not parts:
        return ""
    matched = _safe_int(filters.get("matched"))
    total = _safe_int(filters.get("total"))
    parts.append(f"{matched}/{total} matched")
    return visual_join(parts)


def _session_metrics(counts: dict[str, object]) -> tuple[ProgressMetric, ...]:
    total = _safe_int(counts.get("total"))
    succeeded = _safe_int(counts.get("succeeded"))
    failed = _safe_int(counts.get("failed"))
    skipped = _safe_int(counts.get("skipped"))
    canceled = _safe_int(counts.get("canceled"))
    return (
        ProgressMetric("Succeeded", _ratio_percent(succeeded, total), f"{succeeded} / {total}"),
        ProgressMetric("Failures", _ratio_percent(failed, max(total, 1)), str(failed), "error"),
        ProgressMetric("Skipped", _ratio_percent(skipped, max(total, 1)), str(skipped), "warning"),
        ProgressMetric(
            "Canceled",
            _ratio_percent(canceled, max(total, 1)),
            str(canceled),
            "warning",
        ),
    )


def _session_active_rows(report: dict[str, object]) -> tuple[ActiveWorkRow, ...]:
    items_payload = _dict_value(report, "items")
    return _session_rows_from_items(items_payload.get("sample"))


def _session_rows_from_items(raw_items: object) -> tuple[ActiveWorkRow, ...]:
    if not isinstance(raw_items, list):
        return ()
    rows: list[ActiveWorkRow] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        rows.append(
            ActiveWorkRow(
                item=str(item.get("url") or "-"),
                kind=str(item.get("kind") or "-"),
                phase=str(item.get("status") or "-"),
                progress=str(item.get("message") or item.get("status") or "-"),
                engine=str(item.get("engine") or "-"),
            )
        )
    return tuple(rows)


def _session_scheduler_fields(scheduler: dict[str, object]) -> tuple[ViewField, ...]:
    if not scheduler:
        return ()
    keys = (
        "mode",
        "queue_concurrency",
        "per_host_concurrency",
        "per_file_segments",
        "max_total_connections",
        "max_active_postprocessors",
        "backend",
        "strategy",
    )
    return tuple(
        ViewField(key.replace("_", " ").title(), str(scheduler[key]))
        for key in keys
        if key in scheduler
    )


def _session_failure_rows(report: dict[str, object]) -> tuple[FailureRow, ...]:
    items_payload = _dict_value(report, "items")
    raw_items = items_payload.get("sample")
    if not isinstance(raw_items, list):
        return ()
    failures: list[FailureRow] = []
    for item in raw_items:
        if not isinstance(item, dict) or item.get("status") != DownloadStatus.failed.value:
            continue
        line = str(item.get("line_no") or "-")
        failures.append(
            FailureRow(
                f"Line {line}",
                str(item.get("message") or "failed"),
                "atlas retry SESSION --failed-only",
            )
        )
    return tuple(failures)


def _session_preview_content(
    preview: SessionPreviewChoice,
    *,
    report: dict[str, object],
    payload: dict[str, object],
    manifest: dict[str, object] | None,
) -> tuple[str, str, str] | None:
    if preview == SessionPreviewChoice.none:
        return None
    if preview == SessionPreviewChoice.plan:
        content = json.dumps(
            _session_plan_preview_payload(report, payload, manifest),
            indent=2,
            sort_keys=True,
        )
        return ("Plan JSON", content, "json")
    if preview == SessionPreviewChoice.backend:
        commands = _dict_value(report, "backend_commands")
        sample = commands.get("sample")
        if isinstance(sample, list) and sample:
            content = "\n".join(
                str(item.get("command") or "")
                for item in sample
                if isinstance(item, dict) and item.get("command")
            )
        else:
            content = "No saved backend commands are available for this view."
        return ("Backend Commands", content, "bash")
    if preview == SessionPreviewChoice.manifest:
        content = json.dumps(manifest or payload, indent=2, sort_keys=True)
        return ("Manifest JSON", content, "json")
    if preview == SessionPreviewChoice.summary:
        summary = _dict_value(report, "counts")
        content = json.dumps(summary, indent=2, sort_keys=True)
        return ("Summary JSON", content, "json")
    if preview == SessionPreviewChoice.retry:
        retry = _dict_value(report, "retry")
        content = json.dumps(retry, indent=2, sort_keys=True)
        return ("Retry JSON", content, "json")
    if preview == SessionPreviewChoice.failed:
        failed_urls = _dict_value(_dict_value(report, "retry"), "failed").get("urls")
        content = (
            "\n".join(str(url) for url in failed_urls)
            if isinstance(failed_urls, list)
            else ""
        )
        return ("Failed URLs", content or "No failed URLs", "text")
    if preview == SessionPreviewChoice.errors:
        errors = _dict_value(report, "errors")
        content = json.dumps(errors, indent=2, sort_keys=True)
        return ("Error Report JSON", content, "json")
    if preview == SessionPreviewChoice.logs:
        path = log_dir() / "atlas.log"
        return (
            "Atlas Log",
            _read_text_preview(path, missing=f"No log file found at {path}"),
            "text",
        )
    path = config_path()
    return (
        "Atlas Config",
        _read_text_preview(path, missing=f"No config file found at {path}"),
        "toml",
    )


def _session_plan_preview_payload(
    report: dict[str, object],
    payload: dict[str, object],
    manifest: dict[str, object] | None,
) -> dict[str, object]:
    smart_session = _session_smart_payload(payload, manifest)
    preview: dict[str, object] = {
        "session_type": report.get("session_type"),
        "source": report.get("source"),
        "output_dir": report.get("output_dir"),
        "counts": _dict_value(report, "counts"),
        "filters": _dict_value(report, "filters"),
        "scheduler": _dict_value(report, "scheduler"),
        "commands": _dict_value(report, "commands"),
        "backend_commands": _dict_value(report, "backend_commands"),
    }
    adaptive_plan = (manifest or {}).get("adaptive_plan")
    if isinstance(adaptive_plan, dict):
        preview["adaptive_plan"] = adaptive_plan
    if smart_session:
        preview["smart_session"] = smart_session
    return preview


def _read_text_preview(path: Path, *, missing: str, max_chars: int = 20_000) -> str:
    expanded = path.expanduser()
    if not expanded.exists():
        return missing
    try:
        content = expanded.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read {expanded}: {exc}"
    if len(content) <= max_chars:
        return content or "(empty)"
    return f"... truncated to last {max_chars} characters ...\n{content[-max_chars:]}"


def _session_filtered_urls(
    manifest: dict[str, object] | None,
    *,
    filter_text: str | None,
    status_filter: SessionStatusFilter,
    kind_filter: str | None,
) -> list[str]:
    items = _filter_session_items(
        _session_items(manifest),
        filter_text=filter_text,
        status_filter=status_filter,
        kind_filter=kind_filter,
    )
    urls: list[str] = []
    for item in items:
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url)
    return urls


def _write_url_export(
    path: Path,
    urls: Sequence[str],
    *,
    protected_paths: Sequence[Path] = (),
    force: bool = False,
) -> None:
    """Write a URL export without replacing session inputs or existing data by accident."""

    expanded = path.expanduser()
    if not urls:
        raise AtlasError("No URLs matched; no export file was written.")
    for protected in protected_paths:
        if _paths_alias(expanded, protected.expanduser()):
            raise AtlasError("Export output must not replace a saved session or its artifacts.")
    if expanded.is_symlink():
        raise AtlasError("Refusing to write an export through a symbolic link.")
    if expanded.exists():
        if not expanded.is_file():
            raise AtlasError(f"Export output is not a regular file: {expanded}")
        if not force:
            raise AtlasError(
                f"Export output already exists: {expanded}. Use --force to replace it."
            )
    try:
        expanded.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AtlasError(f"Could not create export folder {expanded.parent}: {exc}") from exc
    text = "\n".join(urls) + "\n"
    if force:
        _replace_export_atomically(expanded, text)
    else:
        _create_export_exclusively(expanded, text)


def _paths_alias(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return first.resolve(strict=False) == second.resolve(strict=False)


def _create_export_exclusively(path: Path, text: str) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AtlasError(
            f"Export output already exists: {path}. Use --force to replace it."
        ) from exc
    except OSError as exc:
        raise AtlasError(f"Could not create export {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise AtlasError(f"Could not write export {path}: {exc}") from exc


def _replace_export_atomically(path: Path, text: str) -> None:
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
            text=True,
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise AtlasError(f"Could not replace export {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _session_artifact_paths(
    session_path: Path,
    payload: Mapping[str, object],
    manifest: Mapping[str, object] | None,
) -> tuple[Path, ...]:
    """Return local session files that an export must never replace."""

    paths = {session_path.expanduser()}
    path_keys = {
        "export_failed_urls",
        "load_manifest",
        "manifest_path",
        "resume_previous_session",
        "save_manifest",
        "summary_path",
    }
    for source in (payload, manifest or {}):
        values: list[object] = [source.get(key) for key in path_keys]
        artifacts = source.get("artifacts")
        if isinstance(artifacts, Mapping):
            values.extend(artifacts.values())
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = session_path.parent / candidate
            paths.add(candidate)
    return tuple(paths)


def _open_saved_session_output(path: Path) -> None:
    expanded = path.expanduser()
    if not expanded.exists():
        raise AtlasError(f"Saved output folder no longer exists: {expanded}")
    if not expanded.is_dir():
        raise AtlasError(f"Saved output path is not a folder: {expanded}")
    open_command = shutil.which("open")
    if not open_command:
        raise AtlasError("Cannot open output folder: macOS 'open' command was not found.")
    subprocess.run([open_command, str(expanded)], check=False)


def _copy_text_to_clipboard(text: str) -> bool:
    pbcopy = shutil.which("pbcopy")
    if not pbcopy:
        return False
    subprocess.run([pbcopy], input=text, text=True, check=False)
    return True


def _safe_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _ratio_percent(value: int, total: int) -> int | None:
    if total <= 0:
        return None
    return min(100, max(0, int((value / total) * 100)))


def _write_mirror_artifacts(
    options: SiteDownloadOptions,
    result: DownloadResult,
    *,
    backend: str,
) -> dict[str, Path]:
    artifact_dir = options.output_dir.expanduser() / ".atlas"
    latest_dir = artifact_dir / "latest"
    manifest_path = latest_dir / "manifest.json"
    summary_path = latest_dir / "summary.json"
    failed_path = latest_dir / "failed.txt"
    skipped_path = latest_dir / "skipped.txt"
    canceled_path = latest_dir / "canceled.txt"
    retry_manifest_path = latest_dir / "retry.atlas.json"
    stats = (result.ydl_opts or {}).get("stats")
    stats_payload = stats if isinstance(stats, dict) else {}
    failed_urls = _mirror_failed_urls_from_stats(stats_payload)
    if result.status == DownloadStatus.failed and not failed_urls:
        failed_urls = [options.url]
    skipped_urls: list[str] = []
    canceled_urls: list[str] = []
    kind = HubKind.dir if isinstance(options, DirectoryMirrorOptions) else HubKind.site
    created_at = datetime.now().isoformat(timespec="seconds")
    summary = {
        "created_at": created_at,
        "kind": kind.value,
        "url": options.url,
        "status": result.status.value,
        "output": str(options.output_dir),
        "backend": backend,
        "failed": len(failed_urls),
        "skipped": len(skipped_urls),
        "canceled": len(canceled_urls),
        "message": result.message,
        "stats_summary": stats_payload.get("summary"),
    }
    manifest = {
        **summary,
        "smart_session": site_session(options, backend=backend).model_dump(mode="json"),
        "items": [
            {
                "line_no": 1,
                "url": options.url,
                "status": result.status.value,
                "kind": kind.value,
                "engine": backend,
                "message": result.message,
            }
        ],
        "artifacts": {
            "summary": str(summary_path),
            "manifest": str(manifest_path),
            "failed": str(failed_path),
            "skipped": str(skipped_path),
            "canceled": str(canceled_path),
            "retry": str(retry_manifest_path),
        },
    }
    retry_manifest = {
        "version": 1,
        "created_at": created_at,
        "kind": kind.value,
        "manifest_path": str(manifest_path),
        "summary_path": str(summary_path),
        "retry_failed_only": failed_urls,
        "retry_checksum_failures_only": [],
        "retry_skipped_unknowns_only": [],
        "retry_canceled_only": canceled_urls,
        "export_failed_urls": str(failed_path),
        "save_manifest": str(manifest_path),
        "load_manifest": str(manifest_path),
        "resume_previous_session": str(manifest_path),
        "skipped_urls": skipped_urls,
        "canceled_urls": canceled_urls,
    }
    latest_paths = _publish_latest_artifacts(
        artifact_dir,
        {
            "summary.json": json.dumps(summary, indent=2, sort_keys=True) + "\n",
            "manifest.json": json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            "failed.txt": ("\n".join(failed_urls) + "\n") if failed_urls else "",
            "skipped.txt": "",
            "canceled.txt": "",
            "retry.atlas.json": json.dumps(retry_manifest, indent=2, sort_keys=True) + "\n",
        },
    )
    return {
        "latest_manifest": latest_paths["manifest.json"],
        "latest_summary": latest_paths["summary.json"],
        "failed": latest_paths["failed.txt"],
        "skipped": latest_paths["skipped.txt"],
        "canceled": latest_paths["canceled.txt"],
        "retry_manifest": latest_paths["retry.atlas.json"],
    }


def _mirror_failed_urls_from_stats(stats: dict[str, object]) -> list[str]:
    site = stats.get("site")
    if not isinstance(site, dict):
        return []
    rows = site.get("rows")
    if not isinstance(rows, list):
        return []
    failed: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = _int_from_stats_value(row.get("status") or row.get("Status"))
        url = str(row.get("url") or row.get("URL") or "").strip()
        if status is not None and status >= 400 and url:
            failed.append(url)
    return _dedupe_urls(failed)


def _int_from_stats_value(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _artifact_panel(paths: dict[str, Path]) -> Panel:
    artifact_table = Table.grid(padding=(0, 2))
    artifact_table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    artifact_table.add_column()
    display_artifacts = [
        ("latest_manifest", "Manifest"),
        ("latest_summary", "Summary"),
    ]
    for key, label in display_artifacts:
        path = paths.get(key)
        if path is not None:
            artifact_table.add_row(label, _display_path(str(path)))
    retry_manifest = paths.get("retry_manifest")
    if retry_manifest is not None and _retry_manifest_has_work(retry_manifest):
        artifact_table.add_row("Retry", _display_path(str(retry_manifest)))
    for key, label in (
        ("failed", "Failed URLs"),
        ("skipped", "Skipped URLs"),
        ("canceled", "Canceled URLs"),
    ):
        path = paths.get(key)
        if path is not None and _artifact_path_has_content(path):
            artifact_table.add_row(label, _display_path(str(path)))
    return Panel(
        artifact_table,
        title=Text("Artifacts", style=ATLAS_TITLE_STYLE),
        border_style=ATLAS_PANEL_STYLE,
        expand=False,
    )


def _artifact_path_has_content(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _retry_manifest_has_work(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    retry_keys = (
        "retry_failed_only",
        "retry_skipped_unknowns_only",
        "retry_canceled_only",
        "retry_checksum_failures_only",
    )
    return any(bool(payload.get(key)) for key in retry_keys)


def _print_artifact_panel(paths: dict[str, Path]) -> None:
    console.print(_artifact_panel(paths))


def _download_status_style(status: DownloadStatus | str) -> str:
    value = status.value if isinstance(status, DownloadStatus) else str(status)
    if value == DownloadStatus.failed.value:
        return ATLAS_ERROR_STYLE
    if value in {DownloadStatus.skipped.value, DownloadStatus.canceled.value}:
        return ATLAS_WARNING_STYLE
    if value == DownloadStatus.dry_run.value:
        return ATLAS_ACTIVE_STYLE
    return ATLAS_SUCCESS_STYLE


def _batch_summary_text(summary: BatchSummary) -> Text:
    parts: list[str | Text | tuple[str, str]] = [
        ("Succeeded: ", ATLAS_MUTED_STYLE),
        (str(summary.succeeded), ATLAS_SUCCESS_STYLE),
    ]
    if summary.failed:
        parts.extend(
            [
                "  ",
                ("Failed: ", ATLAS_MUTED_STYLE),
                (str(summary.failed), ATLAS_ERROR_STYLE),
            ]
        )
    if summary.skipped:
        parts.extend(
            [
                "  ",
                ("Skipped: ", ATLAS_MUTED_STYLE),
                (str(summary.skipped), ATLAS_WARNING_STYLE),
            ]
        )
    if summary.canceled:
        parts.extend(
            [
                "  ",
                ("Canceled: ", ATLAS_MUTED_STYLE),
                (str(summary.canceled), ATLAS_WARNING_STYLE),
            ]
        )
    return Text.assemble(*parts)


def _batch_result_plan_value(plan: dict[str, object] | None, key: str) -> str:
    value = _plain_batch_result_plan_value(plan, key)
    return escape(value) if value != "-" else value


def _plain_batch_result_plan_value(plan: dict[str, object] | None, key: str) -> str:
    if not plan:
        return "-"
    if key == "engine":
        result = plan.get("result")
        if isinstance(result, dict):
            backend = result.get("backend")
            if backend:
                return str(backend)
    route = plan.get("route")
    if not isinstance(route, dict):
        return "-"
    value = route.get(key)
    return str(value) if value else "-"


@app.command("menu")
def menu_command() -> None:
    """Open the interactive keyboard-navigable launcher."""

    _launch_menu(force=True)


_PASSTHROUGH_CONTEXT = {"allow_extra_args": True, "ignore_unknown_options": True}


@app.command("ytdlp", context_settings=_PASSTHROUGH_CONTEXT)
def ytdlp_command(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show command plan only.")] = False,
    backend_help: Annotated[
        bool,
        typer.Option("--backend-help", help="Show yt-dlp backend help."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Capture output unless failed."),
    ] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Advanced yt-dlp pass-through. Put backend args after --."""

    _run_backend_passthrough(
        BackendTool.ytdlp,
        list(ctx.args),
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        timeout=timeout,
        verbose=verbose,
        backend_help=backend_help,
    )


@app.command("aria2", context_settings=_PASSTHROUGH_CONTEXT)
def aria2_command(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show command plan only.")] = False,
    backend_help: Annotated[
        bool,
        typer.Option("--backend-help", help="Show aria2c backend help."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Capture output unless failed."),
    ] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Advanced aria2c pass-through. Put backend args after --."""

    _run_backend_passthrough(
        BackendTool.aria2,
        list(ctx.args),
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        timeout=timeout,
        verbose=verbose,
        backend_help=backend_help,
    )


@app.command("wget", context_settings=_PASSTHROUGH_CONTEXT)
def wget_command(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show command plan only.")] = False,
    backend_help: Annotated[
        bool,
        typer.Option("--backend-help", help="Show wget backend help."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Capture output unless failed."),
    ] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Advanced wget pass-through. Put backend args after --."""

    _run_backend_passthrough(
        BackendTool.wget,
        list(ctx.args),
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        timeout=timeout,
        verbose=verbose,
        backend_help=backend_help,
    )


@app.command("wget2", context_settings=_PASSTHROUGH_CONTEXT)
def wget2_command(
    ctx: typer.Context,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show command plan only.")] = False,
    backend_help: Annotated[
        bool,
        typer.Option("--backend-help", help="Show wget2 backend help."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="Capture output unless failed."),
    ] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Advanced wget2 pass-through. Put backend args after --."""

    _run_backend_passthrough(
        BackendTool.wget2,
        list(ctx.args),
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        timeout=timeout,
        verbose=verbose,
        backend_help=backend_help,
    )


@app.command()
def video(
    url: Annotated[str, typer.Argument(help="YouTube or Rumble URL.")],
    quality: Annotated[
        QualityIntent,
        typer.Option("--quality", help="Outcome preset."),
    ] = QualityIntent.max,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    container: Annotated[
        Container | None, typer.Option("--container", help="Merged video container.")
    ] = None,
    resolution: Annotated[
        ResolutionChoice,
        typer.Option("--resolution", help="Maximum video resolution."),
    ] = ResolutionChoice.max,
    video_codec: Annotated[
        VideoCodecChoice,
        typer.Option("--video-codec", help="Preferred video codec family."),
    ] = VideoCodecChoice.auto,
    hdr: Annotated[HdrChoice, typer.Option("--hdr", help="HDR handling.")] = HdrChoice.auto,
    fps: Annotated[FpsChoice, typer.Option("--fps", help="Maximum FPS.")] = FpsChoice.max,
    aria2: Annotated[
        bool | None, typer.Option("--aria2/--no-aria2", help="Use aria2c for HTTP/HTTPS.")
    ] = None,
    download_engine: Annotated[
        DownloadEngineChoice | None,
        typer.Option("--download-engine", help="Downloader planner mode."),
    ] = None,
    connections: Annotated[int, typer.Option("--connections", min=1, max=64)] = 16,
    splits: Annotated[int, typer.Option("--splits", min=1, max=64)] = 16,
    chunk_size: Annotated[str, typer.Option("--chunk-size")] = "1M",
    archive_path: Annotated[
        Path | None, typer.Option("--archive", help="Download archive path.")
    ] = None,
    no_archive: Annotated[bool, typer.Option("--no-archive", help="Disable archive.")] = False,
    browser_cookies: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", "--browser-cookies", help="Read browser cookies."),
    ] = None,
    cookies_file: Annotated[
        Path | None,
        typer.Option("--cookies-file", help="Netscape cookies file."),
    ] = None,
    playlist: Annotated[
        bool,
        typer.Option("--playlist", help="Allow explicit playlist URL downloads."),
    ] = False,
    playlist_items: Annotated[str | None, typer.Option("--playlist-items")] = None,
    playlist_start: Annotated[int | None, typer.Option("--playlist-start", min=1)] = None,
    playlist_end: Annotated[int | None, typer.Option("--playlist-end", min=1)] = None,
    organize: Annotated[OrganizeMode, typer.Option("--organize")] = OrganizeMode.channel_date,
    filename_template: Annotated[str | None, typer.Option("--filename-template")] = None,
    restrict_filenames: Annotated[bool, typer.Option("--restrict-filenames")] = False,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite/--no-overwrite", help="Overwrite existing files."),
    ] = False,
    continue_download: Annotated[
        bool,
        typer.Option("--continue/--no-continue", help="Resume partial downloads."),
    ] = True,
    metadata: Annotated[
        bool | None, typer.Option("--metadata/--no-metadata", help="Embed metadata.")
    ] = None,
    thumbnail: Annotated[
        bool | None, typer.Option("--thumbnail/--no-thumbnail", help="Write and embed thumbnail.")
    ] = None,
    info_json: Annotated[
        bool | None, typer.Option("--info-json/--no-info-json", help="Write .info.json.")
    ] = None,
    skip_download: Annotated[
        bool,
        typer.Option("--skip-download", help="Skip media transfer and write requested sidecars."),
    ] = False,
    subtitle_only: Annotated[
        bool,
        typer.Option("--subtitle-only", help="Download subtitles only; implies --skip-download."),
    ] = False,
    thumbnail_only: Annotated[
        bool,
        typer.Option("--thumbnail-only", help="Download thumbnail only; implies --skip-download."),
    ] = False,
    info_only: Annotated[
        bool,
        typer.Option("--info-only", help="Write info JSON only; implies --skip-download."),
    ] = False,
    subs: Annotated[
        SubtitleMode,
        typer.Option("--subs", help="Subtitle selection."),
    ] = SubtitleMode.none,
    sub_lang: Annotated[str | None, typer.Option("--sub-lang")] = None,
    embed_subs: Annotated[
        bool,
        typer.Option("--embed-subs/--no-embed-subs", help="Embed subtitles when possible."),
    ] = False,
    chapters: Annotated[
        bool,
        typer.Option("--chapters/--no-chapters", help="Preserve chapters in metadata."),
    ] = True,
    split_chapters: Annotated[bool, typer.Option("--split-chapters")] = False,
    retries: Annotated[int, typer.Option("--retries", min=0)] = 10,
    fragment_retries: Annotated[int, typer.Option("--fragment-retries", min=0)] = 10,
    file_access_retries: Annotated[
        int | None,
        typer.Option("--file-access-retries", min=0, help="Retries for local file access errors."),
    ] = None,
    concurrent_fragments: Annotated[
        int | None,
        typer.Option(
            "--concurrent-fragments",
            min=1,
            max=64,
            help="Native HLS/DASH fragments at once.",
        ),
    ] = None,
    retry_sleep: Annotated[
        list[str] | None,
        typer.Option("--retry-sleep", help="Retry sleep, e.g. http:1 or fragment:linear=1::10."),
    ] = None,
    skip_unavailable_fragments: Annotated[
        bool | None,
        typer.Option(
            "--skip-unavailable-fragments/--abort-unavailable-fragments",
            help="Continue or abort when a media fragment is unavailable.",
        ),
    ] = None,
    rate_limit: Annotated[str | None, typer.Option("--rate-limit")] = None,
    throttled_rate: Annotated[
        str | None,
        typer.Option("--throttled-rate", help="Retry when speed stays below this rate."),
    ] = None,
    http_chunk_size: Annotated[
        str | None,
        typer.Option("--http-chunk-size", help="Chunk size for native HTTP downloads."),
    ] = None,
    socket_timeout: Annotated[
        float | None,
        typer.Option("--socket-timeout", min=0, help="yt-dlp socket timeout."),
    ] = None,
    source_address: Annotated[
        str | None,
        typer.Option("--source-address", help="Client IP address to bind for yt-dlp requests."),
    ] = None,
    impersonate: Annotated[
        str | None,
        typer.Option("--impersonate", help="yt-dlp impersonation target, e.g. chrome."),
    ] = None,
    extractor_args: Annotated[
        list[str] | None,
        typer.Option(
            "--extractor-args",
            "--extractor-arg",
            help="Extractor args, e.g. youtube:player_client=android.",
        ),
    ] = None,
    sleep: Annotated[float | None, typer.Option("--sleep", min=0)] = None,
    proxy: Annotated[str | None, typer.Option("--proxy")] = None,
    match_filters: Annotated[
        list[str] | None,
        typer.Option("--match-filter", "--match-filters", help="yt-dlp selection filter."),
    ] = None,
    break_match_filters: Annotated[
        list[str] | None,
        typer.Option(
            "--break-match-filter",
            "--break-match-filters",
            help="Stop the media queue when this yt-dlp filter rejects an item.",
        ),
    ] = None,
    max_downloads: Annotated[
        int | None,
        typer.Option("--max-downloads", min=1, help="Abort after this many media downloads."),
    ] = None,
    break_on_existing: Annotated[
        bool | None,
        typer.Option(
            "--break-on-existing/--no-break-on-existing",
            help="Stop when an item is already in the download archive.",
        ),
    ] = None,
    break_on_reject: Annotated[
        bool | None,
        typer.Option("--break-on-reject/--no-break-on-reject", help="Stop on rejected media."),
    ] = None,
    break_per_input: Annotated[
        bool | None,
        typer.Option(
            "--break-per-input/--no-break-per-input",
            help="Reset break/max-download counters per input URL.",
        ),
    ] = None,
    date: Annotated[str | None, typer.Option("--date", help="Only media from this date.")] = None,
    date_before: Annotated[
        str | None,
        typer.Option("--date-before", "--datebefore", help="Only media on or before this date."),
    ] = None,
    date_after: Annotated[
        str | None,
        typer.Option("--date-after", "--dateafter", help="Only media on or after this date."),
    ] = None,
    min_filesize: Annotated[
        str | None,
        typer.Option("--min-filesize", help="Skip media smaller than this size."),
    ] = None,
    max_filesize: Annotated[
        str | None,
        typer.Option("--max-filesize", help="Skip media larger than this size."),
    ] = None,
    reject_live: Annotated[
        bool | None,
        typer.Option("--reject-live/--allow-live", help="Skip active livestreams."),
    ] = None,
    reject_upcoming: Annotated[
        bool | None,
        typer.Option("--reject-upcoming/--allow-upcoming", help="Skip upcoming livestreams."),
    ] = None,
    live_from_start: Annotated[
        bool | None,
        typer.Option(
            "--live-from-start/--no-live-from-start",
            help="Download livestreams from start.",
        ),
    ] = None,
    download_sections: Annotated[
        list[str] | None,
        typer.Option(
            "--download-section",
            "--download-sections",
            help='Chapter regex or time range such as "*10:15-inf"; repeatable.',
        ),
    ] = None,
    sponsorblock_mark: Annotated[
        list[str] | None,
        typer.Option("--sponsorblock-mark", help="SponsorBlock categories to mark as chapters."),
    ] = None,
    sponsorblock_remove: Annotated[
        list[str] | None,
        typer.Option("--sponsorblock-remove", help="SponsorBlock categories to cut from media."),
    ] = None,
    sponsorblock_chapter_title: Annotated[
        str | None,
        typer.Option("--sponsorblock-chapter-title", help="Template for SponsorBlock chapters."),
    ] = None,
    sponsorblock_api: Annotated[
        str | None,
        typer.Option("--sponsorblock-api", help="SponsorBlock API base URL."),
    ] = None,
    custom_format: Annotated[
        str | None, typer.Option("--format", "-f", help="Custom yt-dlp format expression.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print yt-dlp options only.")] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Assume yes for future prompts."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Reduce human output.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Download max-quality video."""

    configure_logging(verbose)
    settings = _settings()
    archive_enabled, archive_file = _archive_settings(
        settings=settings,
        archive_path=archive_path,
        no_archive=no_archive,
        overwrite=overwrite,
    )
    option_kwargs = {
        "url": url,
        "output_dir": output_dir or settings.output_dir,
        "archive": archive_enabled,
        "archive_file": archive_file,
        "cookies_file": cookies_file,
        "use_aria2": _use_aria2(settings, aria2),
        "download_engine": _download_engine(selected=download_engine, aria2=aria2),
        "connections": connections,
        "splits": splits,
        "chunk_size": chunk_size,
        "browser_cookies": browser_cookies,
        "playlist": playlist,
        "playlist_items": playlist_items,
        "playlist_start": playlist_start,
        "playlist_end": playlist_end,
        "organize": organize,
        "filename_template": filename_template,
        "restrict_filenames": restrict_filenames,
        "overwrite": overwrite,
        "continue_download": continue_download,
        "retries": retries,
        "fragment_retries": fragment_retries,
        "file_access_retries": (
            settings.media_file_access_retries
            if file_access_retries is None
            else file_access_retries
        ),
        "concurrent_fragments": (
            settings.media_concurrent_fragments
            if concurrent_fragments is None
            else concurrent_fragments
        ),
        "retry_sleep": retry_sleep or settings.media_retry_sleep,
        "skip_unavailable_fragments": _bool_override(
            skip_unavailable_fragments,
            settings.media_skip_unavailable_fragments,
        ),
        "rate_limit": rate_limit,
        "throttled_rate": throttled_rate or settings.media_throttled_rate,
        "http_chunk_size": http_chunk_size or settings.media_http_chunk_size,
        "socket_timeout": (
            socket_timeout if socket_timeout is not None else settings.media_socket_timeout
        ),
        "source_address": source_address or settings.media_source_address,
        "impersonate": impersonate or settings.media_impersonate,
        "extractor_args": extractor_args or settings.media_extractor_args,
        "sleep": sleep,
        "proxy": proxy,
        "match_filters": match_filters or settings.media_match_filters,
        "break_match_filters": break_match_filters or settings.media_break_match_filters,
        "max_downloads": (
            max_downloads if max_downloads is not None else settings.media_max_downloads
        ),
        "break_on_existing": _bool_override(
            break_on_existing,
            settings.media_break_on_existing,
        ),
        "break_on_reject": _bool_override(break_on_reject, settings.media_break_on_reject),
        "break_per_input": _bool_override(break_per_input, settings.media_break_per_input),
        "date": date or settings.media_date,
        "date_before": date_before or settings.media_date_before,
        "date_after": date_after or settings.media_date_after,
        "min_filesize": min_filesize or settings.media_min_filesize,
        "max_filesize": max_filesize or settings.media_max_filesize,
        "reject_live": _bool_override(reject_live, settings.media_reject_live),
        "reject_upcoming": _bool_override(reject_upcoming, settings.media_reject_upcoming),
        "live_from_start": _bool_override(live_from_start, settings.media_live_from_start),
        "download_sections": download_sections or settings.media_download_sections,
        "sponsorblock_mark": sponsorblock_mark or settings.media_sponsorblock_mark,
        "sponsorblock_remove": sponsorblock_remove or settings.media_sponsorblock_remove,
        "sponsorblock_chapter_title": (
            sponsorblock_chapter_title or settings.media_sponsorblock_chapter_title
        ),
        "sponsorblock_api": sponsorblock_api or settings.media_sponsorblock_api,
        "write_info_json": _bool_override(info_json, settings.write_info_json),
        "write_thumbnail": _bool_override(thumbnail, settings.write_thumbnail),
        "embed_thumbnail": _bool_override(thumbnail, settings.embed_thumbnail),
        "embed_metadata": _bool_override(metadata, settings.embed_metadata),
        "skip_download": skip_download,
        "subtitle_only": subtitle_only,
        "thumbnail_only": thumbnail_only,
        "info_only": info_only,
        "subtitle_mode": subs,
        "sub_lang": sub_lang,
        "embed_subs": embed_subs,
        "chapters": chapters,
        "split_chapters": split_chapters,
        "dry_run": dry_run,
        "quiet": quiet,
        "json_output": json_output,
        "progress_mode": progress_mode,
        "verbose": verbose,
        "container": container or settings.video_container,
        "quality": quality,
        "resolution": resolution,
        "video_codec": video_codec,
        "hdr": hdr,
        "fps": fps,
        "format": custom_format,
    }
    try:
        options = VideoDownloadOptions.model_validate(option_kwargs)
    except ValidationError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc
    _ = yes
    _execute_options_or_exit(settings, options, HubKind.video)


@app.command(name="file")
def file_download(
    url: Annotated[str, typer.Argument(help="Direct file URL.")],
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    backend: Annotated[
        FileBackendChoice,
        typer.Option("--backend", help="Direct-file backend."),
    ] = FileBackendChoice.auto,
    filename: Annotated[
        str | None, typer.Option("--filename", help="Override saved file name.")
    ] = None,
    trust_server_names: Annotated[
        bool | None,
        typer.Option(
            "--trust-server-names/--no-trust-server-names",
            help="Use the redirect target filename when no explicit filename is set.",
        ),
    ] = None,
    content_disposition: Annotated[
        bool | None,
        typer.Option(
            "--content-disposition/--no-content-disposition",
            help="Allow Content-Disposition to choose the saved filename.",
        ),
    ] = None,
    timestamping: Annotated[
        bool | None,
        typer.Option(
            "--timestamping/--no-timestamping",
            help="Skip download when the local file is current.",
        ),
    ] = None,
    use_server_timestamps: Annotated[
        bool | None,
        typer.Option(
            "--use-server-timestamps/--no-use-server-timestamps",
            help="Preserve the remote Last-Modified timestamp locally.",
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            min=0,
            help="Network timeout for native, aria2, and wget2 backends.",
        ),
    ] = None,
    connect_timeout: Annotated[
        float | None,
        typer.Option("--connect-timeout", min=0, help="aria2/wget2 connection timeout."),
    ] = None,
    connections: Annotated[int, typer.Option("--connections", min=1, max=64)] = 16,
    splits: Annotated[int, typer.Option("--splits", min=1, max=64)] = 16,
    chunk_size: Annotated[
        str,
        typer.Option("--chunk-size", help="aria2 split size or wget2 chunk size."),
    ] = "1M",
    overwrite: Annotated[bool, typer.Option("--overwrite", help="Replace existing file.")] = False,
    no_continue: Annotated[
        bool, typer.Option("--no-continue", help="Disable resume behavior.")
    ] = False,
    rate_limit: Annotated[str | None, typer.Option("--rate-limit")] = None,
    lowest_speed_limit: Annotated[
        str | None,
        typer.Option("--lowest-speed-limit", help="Abort aria2 downloads below this rate."),
    ] = None,
    max_tries: Annotated[
        int | None,
        typer.Option("--max-tries", min=0, help="aria2 attempts per URI."),
    ] = None,
    retry_wait: Annotated[
        float | None,
        typer.Option("--retry-wait", min=0, help="Seconds to wait between aria2 retries."),
    ] = None,
    checksum: Annotated[
        str | None,
        typer.Option("--checksum", help="Verify digest, e.g. sha256:<hex>."),
    ] = None,
    check_integrity: Annotated[
        bool | None,
        typer.Option("--check-integrity/--no-check-integrity", help="Ask aria2 to verify hashes."),
    ] = None,
    metalink: Annotated[
        bool | None,
        typer.Option(
            "--metalink/--no-metalink",
            help="Expand .meta4/.metalink manifests instead of saving the manifest.",
        ),
    ] = None,
    force_metalink: Annotated[
        bool,
        typer.Option("--force-metalink", help="Treat the URL as a Metalink manifest."),
    ] = False,
    input_file: Annotated[
        Path | None,
        typer.Option("--input-file", help="Load aria2 URLs/session entries from a file."),
    ] = None,
    save_session: Annotated[
        Path | None,
        typer.Option("--save-session", help="Write aria2 session state for restart recovery."),
    ] = None,
    save_session_interval: Annotated[
        int | None,
        typer.Option("--save-session-interval", min=0, help="Seconds between session saves."),
    ] = None,
    metalink_preferred_protocol: Annotated[
        MetalinkPreferredProtocol | None,
        typer.Option("--metalink-preferred-protocol", help="Preferred Metalink mirror protocol."),
    ] = None,
    metalink_language: Annotated[
        str | None,
        typer.Option("--metalink-language", help="Preferred Metalink language."),
    ] = None,
    metalink_os: Annotated[
        str | None,
        typer.Option("--metalink-os", help="Preferred Metalink operating system."),
    ] = None,
    metalink_location: Annotated[
        str | None,
        typer.Option("--metalink-location", help="Preferred Metalink location code."),
    ] = None,
    metalink_base_uri: Annotated[
        str | None,
        typer.Option("--metalink-base-uri", help="Base URI for relative Metalink URLs."),
    ] = None,
    metalink_enable_unique_protocol: Annotated[
        bool | None,
        typer.Option(
            "--metalink-enable-unique-protocol/--no-metalink-enable-unique-protocol",
            help="Allow one URI per protocol per Metalink mirror.",
        ),
    ] = None,
    server_stat_if: Annotated[
        Path | None,
        typer.Option("--server-stat-if", help="Load aria2 server performance profile."),
    ] = None,
    server_stat_of: Annotated[
        Path | None,
        typer.Option("--server-stat-of", help="Save aria2 server performance profile."),
    ] = None,
    server_stat_timeout: Annotated[
        int | None,
        typer.Option("--server-stat-timeout", min=0, help="Seconds before server stats expire."),
    ] = None,
    uri_selector: Annotated[
        Aria2UriSelector | None,
        typer.Option("--uri-selector", help="aria2 mirror selection algorithm."),
    ] = None,
    user_agent: Annotated[str | None, typer.Option("--user-agent")] = None,
    headers: Annotated[
        list[str] | None,
        typer.Option("--header", help="HTTP header, repeatable, e.g. 'Name: value'."),
    ] = None,
    referer: Annotated[str | None, typer.Option("--referer")] = None,
    cache: Annotated[
        bool | None,
        typer.Option("--cache/--no-cache", help="Allow or bypass HTTP caches."),
    ] = None,
    compression: Annotated[
        str | None,
        typer.Option("--compression", help="Accept-Encoding value to request."),
    ] = None,
    no_compression: Annotated[
        bool,
        typer.Option("--no-compression", help="Request uncompressed HTTP responses."),
    ] = False,
    method: Annotated[
        str,
        typer.Option("--method", help="HTTP method for native, aria2, and wget2."),
    ] = "GET",
    body_data: Annotated[str | None, typer.Option("--body-data")] = None,
    body_file: Annotated[Path | None, typer.Option("--body-file")] = None,
    load_cookies: Annotated[Path | None, typer.Option("--load-cookies")] = None,
    proxy: Annotated[str | None, typer.Option("--proxy")] = None,
    file_allocation: Annotated[
        str | None,
        typer.Option(
            "--file-allocation",
            help="aria2 allocation: none, prealloc, trunc, or falloc.",
        ),
    ] = None,
    remote_time: Annotated[
        bool | None,
        typer.Option("--remote-time/--no-remote-time", help="Apply remote timestamps via aria2."),
    ] = None,
    conditional_get: Annotated[
        bool | None,
        typer.Option(
            "--conditional-get/--no-conditional-get",
            help="Enable aria2 conditional GET.",
        ),
    ] = None,
    http_accept_gzip: Annotated[
        bool | None,
        typer.Option(
            "--http-accept-gzip/--no-http-accept-gzip",
            help="Allow aria2 gzip decoding.",
        ),
    ] = None,
    http_user: Annotated[str | None, typer.Option("--http-user")] = None,
    http_password: Annotated[str | None, typer.Option("--http-password")] = None,
    check_certificate: Annotated[
        bool | None,
        typer.Option(
            "--check-certificate/--no-check-certificate",
            help="Validate or bypass HTTPS certificate checks.",
        ),
    ] = None,
    ca_certificate: Annotated[Path | None, typer.Option("--ca-certificate")] = None,
    ca_directory: Annotated[Path | None, typer.Option("--ca-directory")] = None,
    certificate: Annotated[Path | None, typer.Option("--certificate")] = None,
    private_key: Annotated[Path | None, typer.Option("--private-key")] = None,
    secure_protocol: Annotated[str | None, typer.Option("--secure-protocol")] = None,
    adaptive: Annotated[
        bool,
        typer.Option("--adaptive", help="Scan URL metadata and tune concurrency safely."),
    ] = False,
    max_concurrency: Annotated[
        int | None,
        typer.Option("--max-concurrency", min=2, max=100, help="Adaptive global cap."),
    ] = None,
    per_host_concurrency: Annotated[
        int | None,
        typer.Option("--per-host-concurrency", min=1, max=100, help="Adaptive per-host cap."),
    ] = None,
    politeness: Annotated[
        AdaptivePoliteness,
        typer.Option("--politeness", help="Adaptive politeness profile."),
    ] = AdaptivePoliteness.normal,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Scan and print the adaptive plan without downloading."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print backend plan only.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable plan.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Reduce human output.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Download a direct HTTP/HTTPS file through native Python, aria2c, or wget2."""

    configure_logging(verbose)
    settings = _settings()
    options = FileDownloadOptions(
        url=url,
        output_dir=output_dir or settings.output_dir,
        backend=backend if backend != FileBackendChoice.auto else settings.file_backend,
        filename=filename,
        trust_server_names=_bool_override(
            trust_server_names,
            settings.file_trust_server_names,
        ),
        content_disposition=_bool_override(
            content_disposition,
            settings.file_content_disposition,
        ),
        timestamping=_bool_override(timestamping, settings.file_timestamping),
        use_server_timestamps=_bool_override(
            use_server_timestamps,
            settings.file_use_server_timestamps,
        ),
        timeout=timeout if timeout is not None else settings.file_timeout,
        connect_timeout=(
            connect_timeout if connect_timeout is not None else settings.file_connect_timeout
        ),
        connections=connections,
        splits=splits,
        chunk_size=chunk_size,
        overwrite=overwrite,
        continue_download=not no_continue,
        rate_limit=rate_limit,
        lowest_speed_limit=lowest_speed_limit or settings.file_lowest_speed_limit,
        max_tries=max_tries if max_tries is not None else settings.file_max_tries,
        retry_wait=retry_wait if retry_wait is not None else settings.file_retry_wait,
        checksum=checksum,
        check_integrity=_bool_override(check_integrity, settings.file_check_integrity),
        metalink=_bool_override(metalink, True),
        force_metalink=force_metalink,
        input_file=input_file or settings.file_input_file,
        save_session=save_session or settings.file_save_session,
        save_session_interval=(
            save_session_interval
            if save_session_interval is not None
            else settings.file_save_session_interval
        ),
        metalink_preferred_protocol=(
            metalink_preferred_protocol or settings.file_metalink_preferred_protocol
        ),
        metalink_language=metalink_language or settings.file_metalink_language,
        metalink_os=metalink_os or settings.file_metalink_os,
        metalink_location=metalink_location or settings.file_metalink_location,
        metalink_base_uri=metalink_base_uri or settings.file_metalink_base_uri,
        metalink_enable_unique_protocol=(
            metalink_enable_unique_protocol
            if metalink_enable_unique_protocol is not None
            else settings.file_metalink_enable_unique_protocol
        ),
        server_stat_if=server_stat_if or settings.file_server_stat_if,
        server_stat_of=server_stat_of or settings.file_server_stat_of,
        server_stat_timeout=(
            server_stat_timeout
            if server_stat_timeout is not None
            else settings.file_server_stat_timeout
        ),
        uri_selector=uri_selector or settings.file_uri_selector,
        user_agent=user_agent,
        headers=tuple(headers or ()),
        referer=referer,
        cache=cache,
        compression=compression,
        no_compression=no_compression,
        method=method,
        body_data=body_data,
        body_file=body_file,
        load_cookies=load_cookies,
        proxy=proxy,
        file_allocation=file_allocation or settings.file_file_allocation,
        remote_time=_bool_override(remote_time, settings.file_remote_time),
        conditional_get=_bool_override(conditional_get, settings.file_conditional_get),
        http_accept_gzip=_bool_override(http_accept_gzip, settings.file_http_accept_gzip),
        http_user=http_user,
        http_password=http_password,
        check_certificate=check_certificate,
        ca_certificate=ca_certificate,
        ca_directory=ca_directory,
        certificate=certificate,
        private_key=private_key,
        secure_protocol=secure_protocol,
        dry_run=dry_run,
        adaptive=adaptive,
        max_concurrency=max_concurrency,
        per_host_concurrency=per_host_concurrency,
        politeness=politeness,
        explain=explain,
        quiet=quiet,
        json_output=json_output,
        progress_mode=progress_mode,
        verbose=verbose,
    )
    try:
        _execute_options_or_exit(settings, options, HubKind.file)
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc


@app.command()
def site(
    url: Annotated[
        str,
        typer.Argument(help="Website URL to mirror, or 'from-file' for parser input mode."),
    ],
    source: Annotated[
        Path | None,
        typer.Argument(help="Input file when using 'atlas site from-file'."),
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Mirror output directory.")
    ] = None,
    backend: Annotated[
        SiteBackendChoice,
        typer.Option("--backend", help="Website mirror backend."),
    ] = SiteBackendChoice.auto,
    depth: Annotated[int | None, typer.Option("--depth", min=1, max=20)] = None,
    assets: Annotated[
        bool | None,
        typer.Option(
            "--assets/--no-assets",
            "--page-requisites/--no-page-requisites",
            help="Fetch page requisites.",
        ),
    ] = None,
    convert_links: Annotated[
        bool | None,
        typer.Option(
            "--convert-links/--no-convert-links",
            help="Rewrite links for offline use.",
        ),
    ] = None,
    span_hosts: Annotated[
        bool | None,
        typer.Option(
            "--span-hosts/--no-span-hosts",
            help="Allow recursive mirroring across hosts.",
        ),
    ] = None,
    same_host_only: Annotated[
        bool,
        typer.Option("--same-host-only", help="Restrict recursion to the exact seed host."),
    ] = False,
    same_domain_www: Annotated[
        bool,
        typer.Option("--same-domain-www", help="Allow the seed domain and its www variant."),
    ] = False,
    include_subdomains: Annotated[
        bool,
        typer.Option("--include-subdomains", help="Allow domain-bounded subdomain traversal."),
    ] = False,
    wait: Annotated[
        float | None, typer.Option("--wait", min=0, help="Seconds to wait between requests.")
    ] = None,
    accept: Annotated[
        str | None,
        typer.Option("--accept", help="Comma-separated accepted suffixes."),
    ] = None,
    reject: Annotated[
        str | None,
        typer.Option("--reject", help="Comma-separated rejected suffixes."),
    ] = None,
    robots: Annotated[
        bool | None,
        typer.Option("--robots/--no-robots", help="Respect robots.txt for recursive discovery."),
    ] = None,
    follow_sitemaps: Annotated[
        bool | None,
        typer.Option(
            "--follow-sitemaps/--no-follow-sitemaps",
            help="Scan sitemaps discovered through robots.txt.",
        ),
    ] = None,
    no_parent: Annotated[
        bool | None,
        typer.Option("--no-parent/--parent", help="Stay below the starting directory."),
    ] = None,
    domains: Annotated[
        str | None,
        typer.Option("--domains", help="Comma-separated domains to follow."),
    ] = None,
    exclude_domains: Annotated[
        str | None,
        typer.Option("--exclude-domains", help="Comma-separated domains not to follow."),
    ] = None,
    include_directories: Annotated[
        str | None,
        typer.Option(
            "--include-directories",
            help="Comma-separated directory prefixes to download.",
        ),
    ] = None,
    exclude_directories: Annotated[
        str | None,
        typer.Option(
            "--exclude-directories",
            help="Comma-separated directory prefixes not to download.",
        ),
    ] = None,
    accept_regex: Annotated[
        str | None,
        typer.Option("--accept-regex", help="Regex matching accepted URLs."),
    ] = None,
    reject_regex: Annotated[
        str | None,
        typer.Option("--reject-regex", help="Regex matching rejected URLs."),
    ] = None,
    filter_mime_type: Annotated[
        str | None,
        typer.Option("--filter-mime-type", help="Wget2 MIME type allow/deny filter."),
    ] = None,
    filter_urls: Annotated[
        bool,
        typer.Option("--filter-urls", help="Apply accept/reject filters to full URLs."),
    ] = False,
    ignore_case: Annotated[
        bool | None,
        typer.Option("--ignore-case/--case-sensitive", help="Ignore case in filters."),
    ] = None,
    follow_tags: Annotated[
        str | None,
        typer.Option(
            "--follow-tags",
            help="Extra HTML tag/attribute URL sources, e.g. img/data-src.",
        ),
    ] = None,
    ignore_tags: Annotated[
        str | None,
        typer.Option(
            "--ignore-tags",
            help="HTML tag/attribute URL sources to ignore, e.g. img/src,a/href.",
        ),
    ] = None,
    directories: Annotated[
        bool | None,
        typer.Option("--directories/--no-directories", help="Preserve remote directories."),
    ] = None,
    host_directories: Annotated[
        bool | None,
        typer.Option(
            "--host-directories/--no-host-directories",
            help="Include host directories in mirror paths.",
        ),
    ] = None,
    protocol_directories: Annotated[
        bool | None,
        typer.Option(
            "--protocol-directories/--no-protocol-directories",
            help="Include protocol directories in mirror paths.",
        ),
    ] = None,
    cut_dirs: Annotated[int | None, typer.Option("--cut-dirs", min=0)] = None,
    default_page: Annotated[str | None, typer.Option("--default-page")] = None,
    adjust_extension: Annotated[bool, typer.Option("--adjust-extension")] = False,
    convert_file_only: Annotated[bool, typer.Option("--convert-file-only")] = False,
    cut_url_get_vars: Annotated[bool, typer.Option("--cut-url-get-vars")] = False,
    cut_file_get_vars: Annotated[bool, typer.Option("--cut-file-get-vars")] = False,
    keep_extension: Annotated[bool, typer.Option("--keep-extension")] = False,
    unlink: Annotated[bool, typer.Option("--unlink")] = False,
    backups: Annotated[int | None, typer.Option("--backups", min=0)] = None,
    backup_converted: Annotated[bool, typer.Option("--backup-converted")] = False,
    restrict_file_names: Annotated[str | None, typer.Option("--restrict-file-names")] = None,
    download_attr: Annotated[
        DownloadAttrMode | None,
        typer.Option("--download-attr", help="Use Wget2 download attribute path mode."),
    ] = None,
    input_file: Annotated[Path | None, typer.Option("--input-file", "-i")] = None,
    base: Annotated[str | None, typer.Option("--base")] = None,
    force_html: Annotated[bool, typer.Option("--force-html")] = False,
    force_css: Annotated[bool, typer.Option("--force-css")] = False,
    force_sitemap: Annotated[bool, typer.Option("--force-sitemap")] = False,
    force_atom: Annotated[bool, typer.Option("--force-atom")] = False,
    force_rss: Annotated[bool, typer.Option("--force-rss")] = False,
    force_metalink: Annotated[bool, typer.Option("--force-metalink")] = False,
    warc_file: Annotated[Path | None, typer.Option("--warc-file")] = None,
    warc_compression: Annotated[
        bool | None,
        typer.Option("--warc-compression/--no-warc-compression"),
    ] = None,
    warc_cdx: Annotated[bool, typer.Option("--warc-cdx")] = False,
    warc_max_size: Annotated[str | None, typer.Option("--warc-max-size")] = None,
    user_agent: Annotated[str | None, typer.Option("--user-agent")] = None,
    headers: Annotated[
        list[str] | None,
        typer.Option("--header", help="HTTP header, repeatable, e.g. 'Name: value'."),
    ] = None,
    referer: Annotated[str | None, typer.Option("--referer")] = None,
    cache: Annotated[
        bool | None,
        typer.Option("--cache/--no-cache", help="Allow or bypass HTTP caches."),
    ] = None,
    compression: Annotated[str | None, typer.Option("--compression")] = None,
    no_compression: Annotated[bool, typer.Option("--no-compression")] = False,
    method: Annotated[str | None, typer.Option("--method")] = None,
    body_data: Annotated[str | None, typer.Option("--body-data")] = None,
    body_file: Annotated[Path | None, typer.Option("--body-file")] = None,
    post_data: Annotated[str | None, typer.Option("--post-data")] = None,
    post_file: Annotated[Path | None, typer.Option("--post-file")] = None,
    cookies: Annotated[bool | None, typer.Option("--cookies/--no-cookies")] = None,
    cookies_from_browser: Annotated[
        str | None,
        typer.Option(
            "--cookies-from-browser",
            help="Export browser cookies to a temporary Wget2-compatible cookie jar.",
        ),
    ] = None,
    load_cookies: Annotated[Path | None, typer.Option("--load-cookies")] = None,
    save_cookies: Annotated[Path | None, typer.Option("--save-cookies")] = None,
    keep_session_cookies: Annotated[bool, typer.Option("--keep-session-cookies")] = False,
    cookie_suffixes: Annotated[str | None, typer.Option("--cookie-suffixes")] = None,
    netrc: Annotated[bool | None, typer.Option("--netrc/--no-netrc")] = None,
    netrc_file: Annotated[Path | None, typer.Option("--netrc-file")] = None,
    proxy: Annotated[bool | None, typer.Option("--proxy/--no-proxy")] = None,
    http_user: Annotated[str | None, typer.Option("--http-user")] = None,
    http_password: Annotated[str | None, typer.Option("--http-password")] = None,
    proxy_user: Annotated[str | None, typer.Option("--proxy-user")] = None,
    proxy_password: Annotated[str | None, typer.Option("--proxy-password")] = None,
    https_only: Annotated[bool, typer.Option("--https-only")] = False,
    https_enforce: Annotated[HttpsEnforceMode | None, typer.Option("--https-enforce")] = None,
    hsts: Annotated[bool | None, typer.Option("--hsts/--no-hsts")] = None,
    hsts_file: Annotated[Path | None, typer.Option("--hsts-file")] = None,
    check_certificate: Annotated[
        bool | None,
        typer.Option("--check-certificate/--no-check-certificate"),
    ] = None,
    check_hostname: Annotated[
        bool | None,
        typer.Option("--check-hostname/--no-check-hostname"),
    ] = None,
    ca_certificate: Annotated[Path | None, typer.Option("--ca-certificate")] = None,
    ca_directory: Annotated[Path | None, typer.Option("--ca-directory")] = None,
    certificate: Annotated[Path | None, typer.Option("--certificate")] = None,
    certificate_type: Annotated[
        CertificateType | None,
        typer.Option("--certificate-type", help="Client certificate type: PEM or DER."),
    ] = None,
    private_key: Annotated[Path | None, typer.Option("--private-key")] = None,
    private_key_type: Annotated[
        CertificateType | None,
        typer.Option("--private-key-type", help="Private key type: PEM or DER."),
    ] = None,
    crl_file: Annotated[Path | None, typer.Option("--crl-file")] = None,
    secure_protocol: Annotated[str | None, typer.Option("--secure-protocol")] = None,
    ocsp: Annotated[bool | None, typer.Option("--ocsp/--no-ocsp")] = None,
    ocsp_date: Annotated[bool | None, typer.Option("--ocsp-date/--no-ocsp-date")] = None,
    ocsp_file: Annotated[Path | None, typer.Option("--ocsp-file")] = None,
    ocsp_nonce: Annotated[bool | None, typer.Option("--ocsp-nonce/--no-ocsp-nonce")] = None,
    ocsp_server: Annotated[str | None, typer.Option("--ocsp-server")] = None,
    ocsp_stapling: Annotated[
        bool | None,
        typer.Option("--ocsp-stapling/--no-ocsp-stapling"),
    ] = None,
    tls_false_start: Annotated[
        bool | None,
        typer.Option("--tls-false-start/--no-tls-false-start"),
    ] = None,
    tls_resume: Annotated[bool | None, typer.Option("--tls-resume/--no-tls-resume")] = None,
    tls_session_file: Annotated[Path | None, typer.Option("--tls-session-file")] = None,
    http2: Annotated[bool | None, typer.Option("--http2/--no-http2")] = None,
    http2_only: Annotated[bool, typer.Option("--http2-only")] = False,
    http2_request_window: Annotated[
        int | None,
        typer.Option("--http2-request-window", min=1),
    ] = None,
    content_on_error: Annotated[bool, typer.Option("--content-on-error")] = False,
    save_content_on: Annotated[str | None, typer.Option("--save-content-on")] = None,
    save_headers: Annotated[bool, typer.Option("--save-headers")] = False,
    server_response: Annotated[bool, typer.Option("--server-response")] = False,
    ignore_length: Annotated[bool, typer.Option("--ignore-length")] = False,
    verify_sig: Annotated[
        VerifySigMode | None,
        typer.Option("--verify-sig", "-s", help="Verify detached signatures: fail or no-fail."),
    ] = None,
    signature_extensions: Annotated[
        str | None,
        typer.Option("--signature-extensions", help="Comma-separated detached signature suffixes."),
    ] = None,
    gnupg_homedir: Annotated[Path | None, typer.Option("--gnupg-homedir")] = None,
    verify_save_failed: Annotated[bool, typer.Option("--verify-save-failed")] = False,
    max_files: Annotated[
        int | None,
        typer.Option(
            "--max-files",
            min=1,
            help="Fail adaptive scan if discovered items exceed this.",
        ),
    ] = None,
    max_total_size: Annotated[
        str | None,
        typer.Option("--max-total-size", help="Mirror byte cap; friendly alias for Wget2 quota."),
    ] = None,
    max_runtime: Annotated[
        float | None,
        typer.Option("--max-runtime", min=0, help="Stop the mirror subprocess after seconds."),
    ] = None,
    quota: Annotated[str | None, typer.Option("--quota")] = None,
    limit_rate: Annotated[str | None, typer.Option("--limit-rate")] = None,
    retry_connrefused: Annotated[bool, typer.Option("--retry-connrefused")] = False,
    start_pos: Annotated[str | None, typer.Option("--start-pos")] = None,
    inet4_only: Annotated[bool, typer.Option("--inet4-only")] = False,
    inet6_only: Annotated[bool, typer.Option("--inet6-only")] = False,
    bind_address: Annotated[str | None, typer.Option("--bind-address")] = None,
    bind_interface: Annotated[str | None, typer.Option("--bind-interface")] = None,
    prefer_family: Annotated[
        PreferFamily | None,
        typer.Option("--prefer-family", help="Prefer address family: none, IPv4, or IPv6."),
    ] = None,
    dns_cache: Annotated[bool | None, typer.Option("--dns-cache/--no-dns-cache")] = None,
    dns_cache_preload: Annotated[Path | None, typer.Option("--dns-cache-preload")] = None,
    tcp_fastopen: Annotated[
        bool | None,
        typer.Option("--tcp-fastopen/--no-tcp-fastopen"),
    ] = None,
    max_threads: Annotated[
        int | None,
        typer.Option("--max-threads", min=1, max=100, help="Wget2 concurrent threads."),
    ] = None,
    tries: Annotated[
        int | None,
        typer.Option("--tries", min=0, help="Attempts for each download."),
    ] = None,
    waitretry: Annotated[
        float | None,
        typer.Option("--waitretry", min=0, help="Max seconds to wait after a retryable error."),
    ] = None,
    retry_on_http_error: Annotated[
        str | None,
        typer.Option("--retry-on-http-error", help="Comma-separated HTTP status retry list."),
    ] = None,
    max_redirect: Annotated[
        int | None,
        typer.Option("--max-redirect", min=0, help="Maximum redirects to follow."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", min=0, help="General network timeout."),
    ] = None,
    dns_timeout: Annotated[
        float | None,
        typer.Option("--dns-timeout", min=0, help="DNS lookup timeout."),
    ] = None,
    connect_timeout: Annotated[
        float | None,
        typer.Option("--connect-timeout", min=0, help="Connect timeout."),
    ] = None,
    read_timeout: Annotated[
        float | None,
        typer.Option("--read-timeout", min=0, help="Read/write timeout."),
    ] = None,
    random_wait: Annotated[
        bool | None,
        typer.Option("--random-wait/--no-random-wait", help="Jitter waits between requests."),
    ] = None,
    timestamping: Annotated[
        bool | None,
        typer.Option("--timestamping/--no-timestamping", help="Only retrieve newer files."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite/--no-overwrite", help="Overwrite existing mirror files."),
    ] = False,
    continue_download: Annotated[
        bool,
        typer.Option("--continue/--no-continue", help="Resume partial mirror downloads."),
    ] = True,
    check: Annotated[
        bool,
        typer.Option("--check", help="Spider/check mode; discover without saving content."),
    ] = False,
    stats: Annotated[
        bool | None,
        typer.Option("--stats/--no-stats", help="Collect wget2 structured stats files."),
    ] = None,
    adaptive: Annotated[
        bool,
        typer.Option("--adaptive", help="Scan page metadata and tune mirror politeness."),
    ] = False,
    max_concurrency: Annotated[
        int | None,
        typer.Option("--max-concurrency", min=2, max=100, help="Adaptive global cap."),
    ] = None,
    per_host_concurrency: Annotated[
        int | None,
        typer.Option("--per-host-concurrency", min=1, max=100, help="Adaptive per-host cap."),
    ] = None,
    politeness: Annotated[
        AdaptivePoliteness,
        typer.Option("--politeness", help="Adaptive politeness profile."),
    ] = AdaptivePoliteness.normal,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Scan and print the adaptive plan without mirroring."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print backend plan only.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable plan.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Reduce human output.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Mirror a website with wget2 or wget."""

    configure_logging(verbose)
    settings = _settings()
    site_url = url
    site_input_file = input_file
    input_file_only = False
    if url == "from-file":
        if source is None:
            _handle_error(
                AtlasError("atlas site from-file requires an input file path."),
                verbose=verbose,
            )
            raise typer.Exit(1)
        if input_file is not None and input_file != source:
            _handle_error(
                AtlasError("Use either from-file INPUT or --input-file, not both."),
                verbose=verbose,
            )
            raise typer.Exit(1)
        site_url = base or str(source)
        site_input_file = source
        input_file_only = True
    elif source is not None:
        _handle_error(
            AtlasError("A second positional input file is only valid with 'atlas site from-file'."),
            verbose=verbose,
        )
        raise typer.Exit(1)
    try:
        resolved_span_hosts, resolved_domains = _mirror_scope_policy(
            site_url,
            same_host_only=same_host_only,
            same_domain_www=same_domain_www,
            include_subdomains=include_subdomains,
            span_hosts=_bool_override(span_hosts, settings.site_span_hosts),
            domains=domains or settings.site_domains,
        )
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc
    options = SiteDownloadOptions(
        url=site_url,
        output_dir=output_dir or settings.output_dir,
        backend=backend if backend != SiteBackendChoice.auto else settings.site_backend,
        depth=depth if depth is not None else settings.site_depth,
        page_requisites=_bool_override(assets, settings.site_page_requisites),
        convert_links=_bool_override(convert_links, settings.site_convert_links),
        span_hosts=resolved_span_hosts,
        wait=wait if wait is not None else settings.site_wait,
        accept=accept or settings.site_accept,
        reject=reject or settings.site_reject,
        robots=_bool_override(robots, settings.site_robots),
        follow_sitemaps=_bool_override(follow_sitemaps, settings.site_follow_sitemaps),
        no_parent=_bool_override(no_parent, settings.site_no_parent),
        domains=resolved_domains,
        exclude_domains=exclude_domains or settings.site_exclude_domains,
        include_directories=include_directories or settings.site_include_directories,
        exclude_directories=exclude_directories or settings.site_exclude_directories,
        accept_regex=accept_regex or settings.site_accept_regex,
        reject_regex=reject_regex or settings.site_reject_regex,
        filter_mime_type=filter_mime_type or settings.site_filter_mime_type,
        filter_urls=filter_urls,
        ignore_case=_bool_override(ignore_case, settings.site_ignore_case),
        follow_tags=follow_tags,
        ignore_tags=ignore_tags,
        directories=directories,
        host_directories=host_directories,
        protocol_directories=protocol_directories,
        cut_dirs=cut_dirs,
        default_page=default_page,
        adjust_extension=adjust_extension,
        convert_file_only=convert_file_only,
        cut_url_get_vars=cut_url_get_vars,
        cut_file_get_vars=cut_file_get_vars,
        keep_extension=keep_extension,
        unlink=unlink,
        backups=backups,
        backup_converted=backup_converted,
        restrict_file_names=restrict_file_names,
        download_attr=download_attr,
        input_file=site_input_file,
        input_file_only=input_file_only,
        base=base,
        force_html=force_html,
        force_css=force_css,
        force_sitemap=force_sitemap,
        force_atom=force_atom,
        force_rss=force_rss,
        force_metalink=force_metalink,
        warc_file=warc_file,
        warc_compression=warc_compression,
        warc_cdx=warc_cdx,
        warc_max_size=warc_max_size,
        user_agent=user_agent,
        headers=tuple(headers or ()),
        referer=referer,
        cache=cache,
        compression=compression,
        no_compression=no_compression,
        method=method,
        body_data=body_data,
        body_file=body_file,
        post_data=post_data,
        post_file=post_file,
        cookies=cookies,
        browser_cookies=cookies_from_browser,
        load_cookies=load_cookies,
        save_cookies=save_cookies,
        keep_session_cookies=keep_session_cookies,
        cookie_suffixes=cookie_suffixes,
        netrc=netrc,
        netrc_file=netrc_file,
        proxy=proxy,
        http_user=http_user,
        http_password=http_password,
        proxy_user=proxy_user,
        proxy_password=proxy_password,
        https_only=https_only,
        https_enforce=https_enforce,
        hsts=hsts,
        hsts_file=hsts_file,
        check_certificate=check_certificate,
        check_hostname=check_hostname,
        ca_certificate=ca_certificate,
        ca_directory=ca_directory,
        certificate=certificate,
        certificate_type=certificate_type,
        private_key=private_key,
        private_key_type=private_key_type,
        crl_file=crl_file,
        secure_protocol=secure_protocol,
        ocsp=ocsp,
        ocsp_date=ocsp_date,
        ocsp_file=ocsp_file,
        ocsp_nonce=ocsp_nonce,
        ocsp_server=ocsp_server,
        ocsp_stapling=ocsp_stapling,
        tls_false_start=tls_false_start,
        tls_resume=tls_resume,
        tls_session_file=tls_session_file,
        http2=http2,
        http2_only=http2_only,
        http2_request_window=http2_request_window,
        content_on_error=content_on_error,
        save_content_on=save_content_on,
        save_headers=save_headers,
        server_response=server_response,
        ignore_length=ignore_length,
        verify_sig=verify_sig,
        signature_extensions=signature_extensions,
        gnupg_homedir=gnupg_homedir,
        verify_save_failed=verify_save_failed,
        max_files=max_files if max_files is not None else settings.site_max_files,
        max_total_size=max_total_size or settings.site_max_total_size,
        max_runtime=max_runtime if max_runtime is not None else settings.site_max_runtime,
        quota=quota,
        limit_rate=limit_rate,
        retry_connrefused=retry_connrefused,
        start_pos=start_pos,
        inet4_only=inet4_only,
        inet6_only=inet6_only,
        bind_address=bind_address,
        bind_interface=bind_interface,
        prefer_family=prefer_family,
        dns_cache=dns_cache,
        dns_cache_preload=dns_cache_preload,
        tcp_fastopen=tcp_fastopen,
        max_threads=max_threads if max_threads is not None else settings.site_max_threads,
        tries=tries if tries is not None else settings.site_tries,
        waitretry=waitretry if waitretry is not None else settings.site_waitretry,
        retry_on_http_error=retry_on_http_error or settings.site_retry_on_http_error,
        max_redirect=max_redirect if max_redirect is not None else settings.site_max_redirect,
        timeout=timeout if timeout is not None else settings.site_timeout,
        dns_timeout=dns_timeout if dns_timeout is not None else settings.site_dns_timeout,
        connect_timeout=(
            connect_timeout if connect_timeout is not None else settings.site_connect_timeout
        ),
        read_timeout=read_timeout if read_timeout is not None else settings.site_read_timeout,
        random_wait=_bool_override(random_wait, settings.site_random_wait),
        timestamping=_bool_override(timestamping, settings.site_timestamping),
        overwrite=overwrite,
        continue_download=continue_download,
        spider=check,
        stats=_bool_override(stats, settings.site_stats),
        dry_run=dry_run,
        adaptive=adaptive,
        max_concurrency=max_concurrency,
        per_host_concurrency=per_host_concurrency,
        politeness=politeness,
        explain=explain,
        quiet=quiet,
        json_output=json_output,
        progress_mode=progress_mode,
        verbose=verbose,
    )
    _execute_options_or_exit(settings, options, HubKind.site)


@app.command()
def dir(
    url: Annotated[str, typer.Argument(help="Open HTTP directory index or file tree URL.")],
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Mirror output directory.")
    ] = None,
    backend: Annotated[
        SiteBackendChoice,
        typer.Option("--backend", help="Directory mirror backend."),
    ] = SiteBackendChoice.auto,
    depth: Annotated[int | None, typer.Option("--depth", min=1, max=20)] = None,
    accept: Annotated[
        str | None,
        typer.Option("--accept", help="Comma-separated accepted suffixes."),
    ] = None,
    reject: Annotated[
        str | None,
        typer.Option("--reject", help="Comma-separated rejected suffixes."),
    ] = None,
    no_parent: Annotated[
        bool | None,
        typer.Option("--no-parent/--parent", help="Stay below the starting directory."),
    ] = None,
    span_hosts: Annotated[
        bool | None,
        typer.Option("--span-hosts/--no-span-hosts", help="Allow following other hosts."),
    ] = None,
    same_host_only: Annotated[
        bool,
        typer.Option("--same-host-only", help="Restrict recursion to the exact seed host."),
    ] = False,
    same_domain_www: Annotated[
        bool,
        typer.Option("--same-domain-www", help="Allow the seed domain and its www variant."),
    ] = False,
    include_subdomains: Annotated[
        bool,
        typer.Option("--include-subdomains", help="Allow domain-bounded subdomain traversal."),
    ] = False,
    wait: Annotated[
        float | None, typer.Option("--wait", min=0, help="Seconds to wait between requests.")
    ] = None,
    user_agent: Annotated[str | None, typer.Option("--user-agent")] = None,
    if_modified_since: Annotated[
        bool | None,
        typer.Option(
            "--if-modified-since/--no-if-modified-since",
            help="Send or suppress If-Modified-Since request headers in Wget2.",
        ),
    ] = None,
    timestamping: Annotated[
        bool | None,
        typer.Option("--timestamping/--no-timestamping", help="Only retrieve newer files."),
    ] = None,
    max_threads: Annotated[
        int | None,
        typer.Option("--max-threads", min=1, max=100, help="Wget2 concurrent threads."),
    ] = None,
    tries: Annotated[
        int | None,
        typer.Option("--tries", min=0, help="Attempts for each download."),
    ] = None,
    max_redirect: Annotated[
        int | None,
        typer.Option("--max-redirect", min=0, help="Maximum redirects to follow."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", min=0, help="General network timeout."),
    ] = None,
    max_files: Annotated[
        int | None,
        typer.Option(
            "--max-files",
            min=1,
            help="Fail adaptive scan if discovered items exceed this.",
        ),
    ] = None,
    max_total_size: Annotated[
        str | None,
        typer.Option("--max-total-size", help="Mirror byte cap; friendly alias for Wget2 quota."),
    ] = None,
    max_runtime: Annotated[
        float | None,
        typer.Option("--max-runtime", min=0, help="Stop the mirror subprocess after seconds."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite/--no-overwrite", help="Overwrite existing mirror files."),
    ] = False,
    continue_download: Annotated[
        bool,
        typer.Option("--continue/--no-continue", help="Resume partial mirror downloads."),
    ] = True,
    adaptive: Annotated[
        bool,
        typer.Option("--adaptive", help="Scan directory metadata and tune mirror politeness."),
    ] = False,
    max_concurrency: Annotated[
        int | None,
        typer.Option("--max-concurrency", min=2, max=100, help="Adaptive global cap."),
    ] = None,
    per_host_concurrency: Annotated[
        int | None,
        typer.Option("--per-host-concurrency", min=1, max=100, help="Adaptive per-host cap."),
    ] = None,
    politeness: Annotated[
        AdaptivePoliteness,
        typer.Option("--politeness", help="Adaptive politeness profile."),
    ] = AdaptivePoliteness.normal,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Scan and print the adaptive plan without mirroring."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print backend plan only.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable plan.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Reduce human output.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Mirror an explicit open HTTP directory index or file tree."""

    configure_logging(verbose)
    settings = _settings()
    try:
        resolved_span_hosts, resolved_domains = _mirror_scope_policy(
            url,
            same_host_only=same_host_only,
            same_domain_www=same_domain_www,
            include_subdomains=include_subdomains,
            span_hosts=_bool_override(span_hosts, False),
            domains=None,
        )
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc
    options = DirectoryMirrorOptions(
        url=url,
        output_dir=output_dir or settings.output_dir,
        backend=backend if backend != SiteBackendChoice.auto else settings.dir_backend,
        depth=depth if depth is not None else settings.dir_depth,
        accept=accept or settings.site_accept,
        reject=reject or settings.site_reject,
        no_parent=_bool_override(no_parent, True),
        span_hosts=resolved_span_hosts,
        domains=resolved_domains,
        wait=wait if wait is not None else settings.dir_wait,
        robots=settings.site_robots,
        max_threads=max_threads if max_threads is not None else settings.site_max_threads,
        tries=tries if tries is not None else settings.site_tries,
        waitretry=settings.site_waitretry,
        retry_on_http_error=settings.site_retry_on_http_error,
        max_redirect=max_redirect if max_redirect is not None else settings.site_max_redirect,
        timeout=timeout if timeout is not None else settings.site_timeout,
        max_files=max_files if max_files is not None else settings.site_max_files,
        max_total_size=max_total_size or settings.site_max_total_size,
        max_runtime=max_runtime if max_runtime is not None else settings.site_max_runtime,
        dns_timeout=settings.site_dns_timeout,
        connect_timeout=settings.site_connect_timeout,
        read_timeout=settings.site_read_timeout,
        random_wait=settings.site_random_wait,
        timestamping=_bool_override(timestamping, settings.dir_timestamping),
        user_agent=user_agent or settings.dir_user_agent,
        if_modified_since=_bool_override(
            if_modified_since,
            settings.dir_if_modified_since,
        ),
        overwrite=overwrite,
        continue_download=continue_download,
        stats=settings.site_stats,
        dry_run=dry_run,
        adaptive=adaptive,
        max_concurrency=max_concurrency,
        per_host_concurrency=per_host_concurrency,
        politeness=politeness,
        explain=explain,
        quiet=quiet,
        json_output=json_output,
        progress_mode=progress_mode,
        verbose=verbose,
    )
    _execute_options_or_exit(settings, options, HubKind.dir)


@app.command()
def get(
    url: Annotated[str, typer.Argument(help="URL to download through the hub.")],
    kind: Annotated[
        HubKind,
        typer.Option("--kind", help="Outcome to choose; auto is conservative."),
    ] = HubKind.auto,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    backend: Annotated[
        str,
        typer.Option("--backend", help="Backend override for file/site modes."),
    ] = "auto",
    audio: Annotated[bool, typer.Option("--audio", help="Shortcut for --kind audio.")] = False,
    checksum: Annotated[
        str | None,
        typer.Option("--checksum", help="Checksum for file mode, e.g. sha256:<hex>."),
    ] = None,
    video_codec: Annotated[
        VideoCodecChoice,
        typer.Option("--video-codec", help="Preferred video codec when routed to video."),
    ] = VideoCodecChoice.auto,
    codec: Annotated[
        AudioCodec | None,
        typer.Option("--codec", help="Extracted audio codec when routed to audio."),
    ] = None,
    audio_quality: Annotated[
        int | None,
        typer.Option("--audio-quality", min=0, max=10, help="Audio quality when routed to audio."),
    ] = None,
    adaptive: Annotated[
        bool,
        typer.Option("--adaptive", help="Scan direct/site URLs and tune concurrency safely."),
    ] = False,
    max_concurrency: Annotated[
        int | None,
        typer.Option("--max-concurrency", min=2, max=100, help="Adaptive global cap."),
    ] = None,
    per_host_concurrency: Annotated[
        int | None,
        typer.Option("--per-host-concurrency", min=1, max=100, help="Adaptive per-host cap."),
    ] = None,
    politeness: Annotated[
        AdaptivePoliteness,
        typer.Option("--politeness", help="Adaptive politeness profile."),
    ] = AdaptivePoliteness.normal,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Scan and print the adaptive plan without downloading."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print resolved plan only.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable plan.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Reduce human output.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Smart central hub for media, direct files, and explicit site mirrors."""

    configure_logging(verbose)
    settings = _settings()
    try:
        _run_hub_get(
            settings,
            url=url,
            kind=kind,
            output_dir=output_dir,
            backend=backend,
            audio=audio,
            checksum=checksum,
            video_codec=video_codec,
            audio_codec=codec,
            audio_quality=audio_quality,
            dry_run=dry_run,
            adaptive=adaptive,
            max_concurrency=max_concurrency,
            per_host_concurrency=per_host_concurrency,
            politeness=politeness,
            explain=explain,
            json_output=json_output,
            quiet=quiet,
            progress_mode=progress_mode,
            verbose=verbose,
        )
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc


@app.command()
def audio(
    url: Annotated[str, typer.Argument(help="YouTube or Rumble URL.")],
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    codec: Annotated[
        AudioCodec | None,
        typer.Option("--codec", help="Extracted audio codec."),
    ] = None,
    quality: Annotated[int | None, typer.Option("--quality", min=0, max=10)] = None,
    aria2: Annotated[
        bool | None, typer.Option("--aria2/--no-aria2", help="Use aria2c for HTTP/HTTPS.")
    ] = None,
    download_engine: Annotated[
        DownloadEngineChoice | None,
        typer.Option("--download-engine", help="Downloader planner mode."),
    ] = None,
    connections: Annotated[int, typer.Option("--connections", min=1, max=64)] = 16,
    splits: Annotated[int, typer.Option("--splits", min=1, max=64)] = 16,
    chunk_size: Annotated[str, typer.Option("--chunk-size")] = "1M",
    archive_path: Annotated[
        Path | None, typer.Option("--archive", help="Download archive path.")
    ] = None,
    no_archive: Annotated[bool, typer.Option("--no-archive", help="Disable archive.")] = False,
    browser_cookies: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", "--browser-cookies", help="Read browser cookies."),
    ] = None,
    cookies_file: Annotated[
        Path | None,
        typer.Option("--cookies-file", help="Netscape cookies file."),
    ] = None,
    playlist: Annotated[
        bool,
        typer.Option("--playlist", help="Allow explicit playlist URL downloads."),
    ] = False,
    playlist_items: Annotated[str | None, typer.Option("--playlist-items")] = None,
    playlist_start: Annotated[int | None, typer.Option("--playlist-start", min=1)] = None,
    playlist_end: Annotated[int | None, typer.Option("--playlist-end", min=1)] = None,
    organize: Annotated[OrganizeMode, typer.Option("--organize")] = OrganizeMode.channel_date,
    filename_template: Annotated[str | None, typer.Option("--filename-template")] = None,
    restrict_filenames: Annotated[bool, typer.Option("--restrict-filenames")] = False,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite/--no-overwrite", help="Overwrite existing files."),
    ] = False,
    continue_download: Annotated[
        bool,
        typer.Option("--continue/--no-continue", help="Resume partial downloads."),
    ] = True,
    metadata: Annotated[
        bool | None, typer.Option("--metadata/--no-metadata", help="Embed metadata.")
    ] = None,
    thumbnail: Annotated[
        bool | None, typer.Option("--thumbnail/--no-thumbnail", help="Write and embed thumbnail.")
    ] = None,
    info_json: Annotated[
        bool | None, typer.Option("--info-json/--no-info-json", help="Write .info.json.")
    ] = None,
    skip_download: Annotated[
        bool,
        typer.Option("--skip-download", help="Skip media transfer and write requested sidecars."),
    ] = False,
    subtitle_only: Annotated[
        bool,
        typer.Option("--subtitle-only", help="Download subtitles only; implies --skip-download."),
    ] = False,
    thumbnail_only: Annotated[
        bool,
        typer.Option("--thumbnail-only", help="Download thumbnail only; implies --skip-download."),
    ] = False,
    info_only: Annotated[
        bool,
        typer.Option("--info-only", help="Write info JSON only; implies --skip-download."),
    ] = False,
    subs: Annotated[
        SubtitleMode,
        typer.Option("--subs", help="Subtitle selection."),
    ] = SubtitleMode.none,
    sub_lang: Annotated[str | None, typer.Option("--sub-lang")] = None,
    embed_subs: Annotated[
        bool,
        typer.Option("--embed-subs/--no-embed-subs", help="Embed subtitles when possible."),
    ] = False,
    chapters: Annotated[
        bool,
        typer.Option("--chapters/--no-chapters", help="Preserve chapters in metadata."),
    ] = True,
    split_chapters: Annotated[bool, typer.Option("--split-chapters")] = False,
    retries: Annotated[int, typer.Option("--retries", min=0)] = 10,
    fragment_retries: Annotated[int, typer.Option("--fragment-retries", min=0)] = 10,
    file_access_retries: Annotated[
        int | None,
        typer.Option("--file-access-retries", min=0, help="Retries for local file access errors."),
    ] = None,
    concurrent_fragments: Annotated[
        int | None,
        typer.Option(
            "--concurrent-fragments",
            min=1,
            max=64,
            help="Native HLS/DASH fragments at once.",
        ),
    ] = None,
    retry_sleep: Annotated[
        list[str] | None,
        typer.Option("--retry-sleep", help="Retry sleep, e.g. http:1 or fragment:linear=1::10."),
    ] = None,
    skip_unavailable_fragments: Annotated[
        bool | None,
        typer.Option(
            "--skip-unavailable-fragments/--abort-unavailable-fragments",
            help="Continue or abort when a media fragment is unavailable.",
        ),
    ] = None,
    rate_limit: Annotated[str | None, typer.Option("--rate-limit")] = None,
    throttled_rate: Annotated[
        str | None,
        typer.Option("--throttled-rate", help="Retry when speed stays below this rate."),
    ] = None,
    http_chunk_size: Annotated[
        str | None,
        typer.Option("--http-chunk-size", help="Chunk size for native HTTP downloads."),
    ] = None,
    socket_timeout: Annotated[
        float | None,
        typer.Option("--socket-timeout", min=0, help="yt-dlp socket timeout."),
    ] = None,
    source_address: Annotated[
        str | None,
        typer.Option("--source-address", help="Client IP address to bind for yt-dlp requests."),
    ] = None,
    impersonate: Annotated[
        str | None,
        typer.Option("--impersonate", help="yt-dlp impersonation target, e.g. chrome."),
    ] = None,
    extractor_args: Annotated[
        list[str] | None,
        typer.Option(
            "--extractor-args",
            "--extractor-arg",
            help="Extractor args, e.g. youtube:player_client=android.",
        ),
    ] = None,
    sleep: Annotated[float | None, typer.Option("--sleep", min=0)] = None,
    proxy: Annotated[str | None, typer.Option("--proxy")] = None,
    match_filters: Annotated[
        list[str] | None,
        typer.Option("--match-filter", "--match-filters", help="yt-dlp selection filter."),
    ] = None,
    break_match_filters: Annotated[
        list[str] | None,
        typer.Option(
            "--break-match-filter",
            "--break-match-filters",
            help="Stop the media queue when this yt-dlp filter rejects an item.",
        ),
    ] = None,
    max_downloads: Annotated[
        int | None,
        typer.Option("--max-downloads", min=1, help="Abort after this many media downloads."),
    ] = None,
    break_on_existing: Annotated[
        bool | None,
        typer.Option(
            "--break-on-existing/--no-break-on-existing",
            help="Stop when an item is already in the download archive.",
        ),
    ] = None,
    break_on_reject: Annotated[
        bool | None,
        typer.Option("--break-on-reject/--no-break-on-reject", help="Stop on rejected media."),
    ] = None,
    break_per_input: Annotated[
        bool | None,
        typer.Option(
            "--break-per-input/--no-break-per-input",
            help="Reset break/max-download counters per input URL.",
        ),
    ] = None,
    date: Annotated[str | None, typer.Option("--date", help="Only media from this date.")] = None,
    date_before: Annotated[
        str | None,
        typer.Option("--date-before", "--datebefore", help="Only media on or before this date."),
    ] = None,
    date_after: Annotated[
        str | None,
        typer.Option("--date-after", "--dateafter", help="Only media on or after this date."),
    ] = None,
    min_filesize: Annotated[
        str | None,
        typer.Option("--min-filesize", help="Skip media smaller than this size."),
    ] = None,
    max_filesize: Annotated[
        str | None,
        typer.Option("--max-filesize", help="Skip media larger than this size."),
    ] = None,
    reject_live: Annotated[
        bool | None,
        typer.Option("--reject-live/--allow-live", help="Skip active livestreams."),
    ] = None,
    reject_upcoming: Annotated[
        bool | None,
        typer.Option("--reject-upcoming/--allow-upcoming", help="Skip upcoming livestreams."),
    ] = None,
    live_from_start: Annotated[
        bool | None,
        typer.Option(
            "--live-from-start/--no-live-from-start",
            help="Download livestreams from start.",
        ),
    ] = None,
    download_sections: Annotated[
        list[str] | None,
        typer.Option(
            "--download-section",
            "--download-sections",
            help='Chapter regex or time range such as "*10:15-inf"; repeatable.',
        ),
    ] = None,
    sponsorblock_mark: Annotated[
        list[str] | None,
        typer.Option("--sponsorblock-mark", help="SponsorBlock categories to mark as chapters."),
    ] = None,
    sponsorblock_remove: Annotated[
        list[str] | None,
        typer.Option("--sponsorblock-remove", help="SponsorBlock categories to cut from media."),
    ] = None,
    sponsorblock_chapter_title: Annotated[
        str | None,
        typer.Option("--sponsorblock-chapter-title", help="Template for SponsorBlock chapters."),
    ] = None,
    sponsorblock_api: Annotated[
        str | None,
        typer.Option("--sponsorblock-api", help="SponsorBlock API base URL."),
    ] = None,
    custom_format: Annotated[
        str | None, typer.Option("--format", "-f", help="Custom yt-dlp format expression.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print yt-dlp options only.")] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Assume yes for future prompts."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Reduce human output.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Extract audio."""

    configure_logging(verbose)
    settings = _settings()
    archive_enabled, archive_file = _archive_settings(
        settings=settings,
        archive_path=archive_path,
        no_archive=no_archive,
        overwrite=overwrite,
    )
    option_kwargs = {
        "url": url,
        "output_dir": output_dir or settings.output_dir,
        "archive": archive_enabled,
        "archive_file": archive_file,
        "cookies_file": cookies_file,
        "use_aria2": _use_aria2(settings, aria2),
        "download_engine": _download_engine(selected=download_engine, aria2=aria2),
        "connections": connections,
        "splits": splits,
        "chunk_size": chunk_size,
        "browser_cookies": browser_cookies,
        "playlist": playlist,
        "playlist_items": playlist_items,
        "playlist_start": playlist_start,
        "playlist_end": playlist_end,
        "organize": organize,
        "filename_template": filename_template,
        "restrict_filenames": restrict_filenames,
        "overwrite": overwrite,
        "continue_download": continue_download,
        "retries": retries,
        "fragment_retries": fragment_retries,
        "file_access_retries": (
            settings.media_file_access_retries
            if file_access_retries is None
            else file_access_retries
        ),
        "concurrent_fragments": (
            settings.media_concurrent_fragments
            if concurrent_fragments is None
            else concurrent_fragments
        ),
        "retry_sleep": retry_sleep or settings.media_retry_sleep,
        "skip_unavailable_fragments": _bool_override(
            skip_unavailable_fragments,
            settings.media_skip_unavailable_fragments,
        ),
        "rate_limit": rate_limit,
        "throttled_rate": throttled_rate or settings.media_throttled_rate,
        "http_chunk_size": http_chunk_size or settings.media_http_chunk_size,
        "socket_timeout": (
            socket_timeout if socket_timeout is not None else settings.media_socket_timeout
        ),
        "source_address": source_address or settings.media_source_address,
        "impersonate": impersonate or settings.media_impersonate,
        "extractor_args": extractor_args or settings.media_extractor_args,
        "sleep": sleep,
        "proxy": proxy,
        "match_filters": match_filters or settings.media_match_filters,
        "break_match_filters": break_match_filters or settings.media_break_match_filters,
        "max_downloads": (
            max_downloads if max_downloads is not None else settings.media_max_downloads
        ),
        "break_on_existing": _bool_override(
            break_on_existing,
            settings.media_break_on_existing,
        ),
        "break_on_reject": _bool_override(break_on_reject, settings.media_break_on_reject),
        "break_per_input": _bool_override(break_per_input, settings.media_break_per_input),
        "date": date or settings.media_date,
        "date_before": date_before or settings.media_date_before,
        "date_after": date_after or settings.media_date_after,
        "min_filesize": min_filesize or settings.media_min_filesize,
        "max_filesize": max_filesize or settings.media_max_filesize,
        "reject_live": _bool_override(reject_live, settings.media_reject_live),
        "reject_upcoming": _bool_override(reject_upcoming, settings.media_reject_upcoming),
        "live_from_start": _bool_override(live_from_start, settings.media_live_from_start),
        "download_sections": download_sections or settings.media_download_sections,
        "sponsorblock_mark": sponsorblock_mark or settings.media_sponsorblock_mark,
        "sponsorblock_remove": sponsorblock_remove or settings.media_sponsorblock_remove,
        "sponsorblock_chapter_title": (
            sponsorblock_chapter_title or settings.media_sponsorblock_chapter_title
        ),
        "sponsorblock_api": sponsorblock_api or settings.media_sponsorblock_api,
        "write_info_json": _bool_override(info_json, settings.write_info_json),
        "write_thumbnail": _bool_override(thumbnail, settings.write_thumbnail),
        "embed_thumbnail": _bool_override(thumbnail, settings.embed_thumbnail),
        "embed_metadata": _bool_override(metadata, settings.embed_metadata),
        "skip_download": skip_download,
        "subtitle_only": subtitle_only,
        "thumbnail_only": thumbnail_only,
        "info_only": info_only,
        "subtitle_mode": subs,
        "sub_lang": sub_lang,
        "embed_subs": embed_subs,
        "chapters": chapters,
        "split_chapters": split_chapters,
        "dry_run": dry_run,
        "quiet": quiet,
        "json_output": json_output,
        "progress_mode": progress_mode,
        "verbose": verbose,
        "codec": codec or settings.audio_codec,
        "quality": settings.audio_quality if quality is None else quality,
        "format": custom_format,
    }
    try:
        options = AudioDownloadOptions.model_validate(option_kwargs)
    except ValidationError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc
    _ = yes
    _execute_options_or_exit(settings, options, HubKind.audio)


@app.command("playlist")
def playlist_command(
    url: Annotated[str, typer.Argument(help="Explicit playlist URL.")],
    kind: Annotated[
        BatchKind | None,
        typer.Option("--type", help="Download playlist as video or audio."),
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    quality: Annotated[
        QualityIntent,
        typer.Option("--quality", help="Video outcome preset."),
    ] = QualityIntent.max,
    video_codec: Annotated[
        VideoCodecChoice,
        typer.Option("--video-codec", help="Preferred video codec when --type video."),
    ] = VideoCodecChoice.auto,
    codec: Annotated[
        AudioCodec | None,
        typer.Option("--codec", help="Audio codec when --type audio."),
    ] = None,
    audio_quality: Annotated[
        int | None,
        typer.Option("--audio-quality", min=0, max=10, help="Audio extraction quality."),
    ] = None,
    browser_cookies: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", "--browser-cookies", help="Read browser cookies."),
    ] = None,
    cookies_file: Annotated[
        Path | None,
        typer.Option("--cookies-file", help="Netscape cookies file."),
    ] = None,
    playlist_items: Annotated[str | None, typer.Option("--playlist-items")] = None,
    playlist_start: Annotated[int | None, typer.Option("--playlist-start", min=1)] = None,
    playlist_end: Annotated[int | None, typer.Option("--playlist-end", min=1)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print yt-dlp options only.")] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Default to video when no --type is supplied."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Machine-readable dry-run output."),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Reduce human output.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Download an explicit playlist as video or audio."""

    configure_logging(verbose)
    settings = _settings()
    if not is_explicit_playlist_url(url):
        message = (
            "atlas playlist only accepts explicit playlist URLs. "
            "Use atlas video or atlas audio for watch URLs with list= or start_radio= parameters."
        )
        _handle_error(AtlasError(message), verbose=verbose)
        raise typer.Exit(1)
    try:
        resolved_kind = _resolve_playlist_kind(kind, yes=yes, quiet=quiet)
        if resolved_kind == BatchKind.audio:
            audio_options = AudioDownloadOptions.model_validate(
                {
                    "url": url,
                    "output_dir": output_dir or settings.output_dir,
                    "archive": settings.archive,
                    "archive_file": settings.archive_file,
                    "cookies_file": cookies_file,
                    "use_aria2": settings.aria2,
                    "concurrent_fragments": settings.media_concurrent_fragments,
                    "file_access_retries": settings.media_file_access_retries,
                    "retry_sleep": settings.media_retry_sleep,
                    "skip_unavailable_fragments": settings.media_skip_unavailable_fragments,
                    "throttled_rate": settings.media_throttled_rate,
                    "http_chunk_size": settings.media_http_chunk_size,
                    "socket_timeout": settings.media_socket_timeout,
                    "source_address": settings.media_source_address,
                    "impersonate": settings.media_impersonate,
                    "extractor_args": settings.media_extractor_args,
                    "browser_cookies": browser_cookies,
                    "playlist": True,
                    "playlist_items": playlist_items,
                    "playlist_start": playlist_start,
                    "playlist_end": playlist_end,
                    "organize": OrganizeMode.playlist,
                    "write_info_json": settings.write_info_json,
                    "write_thumbnail": settings.write_thumbnail,
                    "embed_thumbnail": settings.embed_thumbnail,
                    "embed_metadata": settings.embed_metadata,
                    "dry_run": dry_run,
                    "quiet": quiet,
                    "json_output": json_output,
                    "progress_mode": progress_mode,
                    "verbose": verbose,
                    "codec": codec or settings.audio_codec,
                    "quality": settings.audio_quality if audio_quality is None else audio_quality,
                }
            )
            _execute_options_or_exit(settings, audio_options, HubKind.audio)
            return
        video_options = VideoDownloadOptions.model_validate(
            {
                "url": url,
                "output_dir": output_dir or settings.output_dir,
                "archive": settings.archive,
                "archive_file": settings.archive_file,
                "cookies_file": cookies_file,
                "use_aria2": settings.aria2,
                "concurrent_fragments": settings.media_concurrent_fragments,
                "file_access_retries": settings.media_file_access_retries,
                "retry_sleep": settings.media_retry_sleep,
                "skip_unavailable_fragments": settings.media_skip_unavailable_fragments,
                "throttled_rate": settings.media_throttled_rate,
                "http_chunk_size": settings.media_http_chunk_size,
                "socket_timeout": settings.media_socket_timeout,
                "source_address": settings.media_source_address,
                "impersonate": settings.media_impersonate,
                "extractor_args": settings.media_extractor_args,
                "browser_cookies": browser_cookies,
                "playlist": True,
                "playlist_items": playlist_items,
                "playlist_start": playlist_start,
                "playlist_end": playlist_end,
                "organize": OrganizeMode.playlist,
                "write_info_json": settings.write_info_json,
                "write_thumbnail": settings.write_thumbnail,
                "embed_thumbnail": settings.embed_thumbnail,
                "embed_metadata": settings.embed_metadata,
                "dry_run": dry_run,
                "quiet": quiet,
                "json_output": json_output,
                "progress_mode": progress_mode,
                "verbose": verbose,
                "container": settings.video_container,
                "quality": quality,
                "video_codec": video_codec,
            }
        )
    except (ValidationError, AtlasError) as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc
    _execute_options_or_exit(settings, video_options, HubKind.video)


@app.command()
def info(
    url: Annotated[str, typer.Argument(help="YouTube or Rumble URL.")],
    browser_cookies: Annotated[
        str | None,
        typer.Option(
            "--cookies-from-browser",
            "--browser-cookies",
            help="Read cookies from browser, e.g. chrome.",
        ),
    ] = None,
    cookies_file: Annotated[
        Path | None,
        typer.Option("--cookies-file", help="Netscape cookies file."),
    ] = None,
    playlist: Annotated[
        bool,
        typer.Option("--playlist", help="Allow explicit playlist URL extraction."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show sanitized media metadata."""

    configure_logging(verbose)
    settings = _settings()
    try:
        media = _probe(_engine(settings)).probe(
            InfoOptions(
                url=url,
                browser_cookies=browser_cookies,
                cookies_file=cookies_file,
                playlist=playlist,
                verbose=verbose,
            )
        )
    except EngineError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc

    if json_output:
        console.print_json(media.model_dump_json())
        return

    _metadata_panel(
        "Media Info",
        [
            ("Title", escape(media.title or "-")),
            ("Uploader", escape(media.uploader or media.channel or "-")),
            ("Duration", format_duration(media.duration)),
            ("Source", _source_label(media.extractor)),
            ("Upload Date", _upload_date(media.upload_date)),
            ("Views", _views(media.view_count)),
            ("URL", escape(media.webpage_url or url)),
            ("Playlist", "yes" if media.is_playlist else "no"),
        ],
    )
    best_video, best_audio = _recommended_formats(media.formats)
    console.print()
    console.print(Text("Best available:", style=ATLAS_TITLE_STYLE))
    console.print(_format_summary_line("Video", _format_summary(best_video)))
    console.print(_format_summary_line("Audio", _format_summary(best_audio)))
    console.print()
    _print_smart_format_choices(media.formats)


@app.command()
def formats(
    url: Annotated[str, typer.Argument(help="YouTube or Rumble URL.")],
    video_only: Annotated[
        bool,
        typer.Option("--video-only", help="Show video formats only."),
    ] = False,
    audio_only: Annotated[
        bool,
        typer.Option("--audio-only", help="Show audio formats only."),
    ] = False,
    sort: Annotated[FormatSort, typer.Option("--sort", help="Sort order.")] = FormatSort.quality,
    browser_cookies: Annotated[
        str | None,
        typer.Option(
            "--cookies-from-browser",
            "--browser-cookies",
            help="Read cookies from browser, e.g. chrome.",
        ),
    ] = None,
    cookies_file: Annotated[
        Path | None,
        typer.Option("--cookies-file", help="Netscape cookies file."),
    ] = None,
    playlist: Annotated[
        bool,
        typer.Option("--playlist", help="Allow explicit playlist URL extraction."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List available formats."""

    configure_logging(verbose)
    settings = _settings()
    if video_only and audio_only:
        _handle_error(
            AtlasError("Choose either --video-only or --audio-only, not both."),
            verbose=verbose,
        )
        raise typer.Exit(1)
    try:
        items = _probe(_engine(settings)).formats(
            InfoOptions(
                url=url,
                browser_cookies=browser_cookies,
                cookies_file=cookies_file,
                playlist=playlist,
                verbose=verbose,
            )
        )
    except EngineError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc

    selected = sort_formats(
        filter_formats(items, video_only=video_only, audio_only=audio_only),
        sort,
    )
    if json_output:
        console.print_json(json.dumps([item.model_dump() for item in selected], default=str))
        return

    best_video, best_audio = _recommended_formats(items)
    recommended_ids = {
        getattr(fmt, "format_id", None) for fmt in (best_video, best_audio) if fmt is not None
    }
    console.print(Text("Available formats", style=ATLAS_TITLE_STYLE))
    if best_video or best_audio:
        if audio_only and not video_only:
            recommended_label = DEFAULT_AUDIO_FORMAT
            recommended_value = _format_summary(best_audio)
        elif video_only and not audio_only:
            recommended_label = "bestvideo*"
            recommended_value = _format_summary(best_video)
        else:
            recommended_label = DEFAULT_VIDEO_FORMAT
            recommended_value = f"{_format_summary(best_video)} + {_format_summary(best_audio)}"
        console.print(_recommended_format_line(recommended_label, recommended_value))
        console.print()
    _print_smart_format_choices(selected)
    if MediaCapabilityResolver.from_info(MediaInfo(formats=list(selected))).all_profiles():
        console.print()
    table = Table(box=table_box(), header_style=ATLAS_MUTED_STYLE)
    for column in (
        "ID",
        "Ext",
        "Res",
        "FPS",
        "VCodec",
        "ACodec",
        "Size",
        "TBR",
        "Note",
    ):
        table.add_column(column)
    for fmt in selected:
        table.add_row(
            fmt.format_id,
            fmt.ext or "-",
            _resolution_label(fmt.resolution),
            f"{fmt.fps:g}" if fmt.fps else "-",
            _codec_label(fmt.vcodec),
            _codec_label(fmt.acodec),
            format_bytes(fmt.filesize),
            f"{fmt.tbr:g}k" if fmt.tbr else "-",
            fmt.note or "-",
            style=ATLAS_ACTIVE_STYLE if fmt.format_id in recommended_ids else None,
        )
    console.print(table)


@app.command()
def batch(
    file: Annotated[Path, typer.Argument(help="Text file containing one URL per line.")],
    kind: Annotated[
        BatchKind,
        typer.Option("--kind", "--type", help="Batch routing mode."),
    ] = BatchKind.auto,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    backend: Annotated[
        str,
        typer.Option("--backend", help="Backend override for file/site batch items."),
    ] = "auto",
    allow_sites: Annotated[
        bool,
        typer.Option("--allow-sites", help="Allow recursive site mirrors in auto batch mode."),
    ] = False,
    allow_dirs: Annotated[
        bool,
        typer.Option(
            "--allow-dirs",
            help="Allow open-directory mirrors in auto or explicit dir batch mode.",
        ),
    ] = False,
    concurrency: Annotated[
        int | None,
        typer.Option(
            "--concurrency",
            "-j",
            min=1,
            max=16,
            help="URLs to download at once. aria2 connections and splits are per URL.",
        ),
    ] = None,
    video_codec: Annotated[
        VideoCodecChoice,
        typer.Option("--video-codec", help="Preferred video codec for video batch items."),
    ] = VideoCodecChoice.auto,
    codec: Annotated[
        AudioCodec | None,
        typer.Option("--codec", help="Extracted audio codec for audio batch items."),
    ] = None,
    audio_quality: Annotated[
        int | None,
        typer.Option("--audio-quality", min=0, max=10, help="Audio quality for audio batch items."),
    ] = None,
    adaptive: Annotated[
        bool,
        typer.Option("--adaptive", help="Scan direct/site items and tune batch concurrency."),
    ] = False,
    max_concurrency: Annotated[
        int | None,
        typer.Option("--max-concurrency", min=2, max=100, help="Adaptive global cap."),
    ] = None,
    per_host_concurrency: Annotated[
        int | None,
        typer.Option("--per-host-concurrency", min=1, max=100, help="Adaptive per-host cap."),
    ] = None,
    politeness: Annotated[
        AdaptivePoliteness,
        typer.Option("--politeness", help="Adaptive politeness profile."),
    ] = AdaptivePoliteness.normal,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Scan and print the adaptive batch plan only."),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print resolved plans only.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable summary.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Download URLs from a file with smart per-URL routing."""

    configure_logging(verbose)
    settings = _settings()
    resolved_concurrency = concurrency or settings.batch_concurrency
    adaptive_plan = None
    adaptive_requested = adaptive or explain
    adaptive_per_host_concurrency: int | None = None
    if adaptive_requested:
        adaptive_plan = _adaptive_batch_plan_from_file(
            settings,
            file=file,
            kind=kind,
            output_dir=output_dir or settings.output_dir,
            backend=backend,
            allow_sites=allow_sites,
            allow_dirs=allow_dirs,
            controls=default_adaptive_controls(
                enabled=True,
                max_concurrency=max_concurrency,
                per_host_concurrency=per_host_concurrency,
                politeness=politeness,
                dry_run=dry_run,
            ),
        )
        if adaptive_plan is not None:
            adaptive_per_host_concurrency = adaptive_plan.per_host_concurrency
            if concurrency is None:
                resolved_concurrency = adaptive_plan.queue_concurrency
        if explain:
            _print_backend_plan(
                {
                    "kind": kind.value,
                    "adaptive": adaptive_plan.model_dump(mode="json") if adaptive_plan else None,
                    "safety": "media items remain on the yt-dlp path",
                },
                json_output=json_output,
                explain=True,
            )
            return
    try:
        entries_for_names, _skipped_for_names = load_batch_file(file)
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc
    filename_overrides = _batch_file_filename_overrides(entries_for_names)
    adaptive_items_by_url = _adaptive_work_item_by_url(adaptive_plan)
    active_reporter: BatchProgressReporter | None = None
    active_runtime_scheduler: AdaptiveScheduler | None = None
    batch_control = BatchControl()
    batch_operator_controller = BatchOperatorController(batch_control)

    def handler(
        entry: BatchEntry,
        progress_hooks: list[ProgressHook] | None,
        context: BatchItemContext | None = None,
    ) -> DownloadResult:
        planned = _batch_hub_plan_from_url(
            settings,
            url=entry.url,
            kind=kind,
            output_dir=output_dir or settings.output_dir,
            backend=backend,
            dry_run=dry_run,
            json_output=json_output,
            verbose=verbose,
            allow_sites=allow_sites,
            allow_dirs=allow_dirs,
            video_codec=video_codec,
            audio_codec=codec,
            audio_quality=audio_quality,
            adaptive=adaptive_requested,
            max_concurrency=max_concurrency,
            per_host_concurrency=per_host_concurrency,
            politeness=politeness,
            explain=False,
        )
        if isinstance(planned, DownloadResult):
            _emit_batch_result_event(
                active_reporter,
                entry,
                planned,
                adaptive_plan=adaptive_plan,
                adaptive_scheduler=active_runtime_scheduler,
                adaptive_items_by_url=adaptive_items_by_url,
            )
            return planned
        planned = _apply_batch_filename_override(
            settings,
            planned,
            filename_overrides,
            entry,
        )
        try:
            result = _run_batch_hub_plan(
                settings,
                planned,
                progress_hooks=progress_hooks,
                postprocessor_hooks=_batch_postprocessor_hooks(
                    active_reporter,
                    entry,
                    planned.route.kind,
                ),
                progress_callback=_batch_progress_callback(
                    active_reporter,
                    entry,
                    adaptive_plan=adaptive_plan,
                    adaptive_scheduler=active_runtime_scheduler,
                    adaptive_items_by_url=adaptive_items_by_url,
                ),
                process_control=context.process_control if context is not None else None,
            )
        except Exception as exc:
            _emit_batch_result_event(
                active_reporter,
                entry,
                DownloadResult(
                    status=DownloadStatus.failed,
                    url=entry.url,
                    message=str(exc),
                ),
                plan=planned,
                adaptive_plan=adaptive_plan,
                adaptive_scheduler=active_runtime_scheduler,
                adaptive_items_by_url=adaptive_items_by_url,
            )
            raise
        _emit_batch_result_event(
            active_reporter,
            entry,
            result,
            plan=planned,
            adaptive_plan=adaptive_plan,
            adaptive_scheduler=active_runtime_scheduler,
            adaptive_items_by_url=adaptive_items_by_url,
        )
        return result

    def run_runtime_batch(
        reporter: BatchProgressReporter | None = None,
    ) -> BatchSummary:
        nonlocal active_runtime_scheduler
        active_runtime_scheduler = (
            _adaptive_batch_runtime_scheduler(adaptive_plan)
            if adaptive_plan is not None
            else None
        )
        hook_factory = (
            (
                lambda entry: create_batch_progress_hook(
                    reporter,
                    line_no=entry.line_no,
                    url=entry.url,
                )
            )
            if reporter is not None
            else None
        )
        if adaptive_plan is not None:
            assert active_runtime_scheduler is not None
            return run_batch_adaptive(
                file,
                kind,
                handler,
                scheduler=active_runtime_scheduler,
                progress_hook_factory=hook_factory,
                control=batch_control,
            )
        return run_batch_concurrent(
            file,
            kind,
            handler,
            concurrency=resolved_concurrency,
            per_host_concurrency=adaptive_per_host_concurrency,
            progress_hook_factory=hook_factory,
            control=batch_control,
        )

    def run_shared_batch(
        reporter: BatchProgressReporter | None = None,
    ) -> BatchSummary | None:
        return _try_run_aria2_batch_queue(
            settings,
            file=file,
            kind=kind,
            output_dir=output_dir or settings.output_dir,
            backend=backend,
            allow_sites=allow_sites,
            allow_dirs=allow_dirs,
            resolved_concurrency=resolved_concurrency,
            video_codec=video_codec,
            audio_codec=codec,
            audio_quality=audio_quality,
            adaptive=adaptive_requested,
            max_concurrency=max_concurrency,
            per_host_concurrency=per_host_concurrency,
            politeness=politeness,
            verbose=verbose,
            reporter=reporter,
            adaptive_plan=adaptive_plan,
            filename_overrides=filename_overrides,
            adaptive_items_by_url=adaptive_items_by_url,
        )

    try:
        summary: BatchSummary
        if dry_run:
            summary = run_batch_concurrent(
                file,
                kind,
                handler,
                concurrency=resolved_concurrency,
                per_host_concurrency=adaptive_per_host_concurrency,
            )
        elif json_output:
            shared_summary = run_shared_batch()
            summary = shared_summary if shared_summary is not None else run_runtime_batch()
        else:
            entries, _skipped = load_batch_file(file)
            console.print(
                Text(
                    f"Batch {kind.value}: concurrency {resolved_concurrency}",
                    style=ATLAS_MUTED_STYLE,
                )
            )
            with _batch_progress_reporter(
                concurrency=resolved_concurrency,
                progress_mode=resolve_progress_mode(
                    progress_mode,
                    console=console,
                    quiet=False,
                    json_output=False,
                ),
                total=len(entries),
                work_context=_batch_work_context(
                    queue_count=len(entries),
                    concurrency=resolved_concurrency,
                    allow_sites=allow_sites,
                    allow_dirs=allow_dirs,
                    output_dir=output_dir or settings.output_dir,
                    adaptive_plan=adaptive_plan,
                ),
            ) as reporter:
                reporter.seed_entries(entries, kind=_batch_hub_kind(kind))
                active_reporter = reporter
                try:
                    shared_summary = run_shared_batch(reporter)
                    if shared_summary is None:
                        reporter.operator_controller = batch_operator_controller
                        summary = run_runtime_batch(reporter)
                    else:
                        summary = shared_summary
                finally:
                    active_reporter = None
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc

    artifact_paths: dict[str, Path] = {}
    if not dry_run:
        try:
            artifact_paths = _write_batch_artifacts(
                summary,
                output_dir=output_dir or settings.output_dir,
                adaptive_plan=adaptive_plan,
                source=str(file),
            )
        except OSError as exc:
            _handle_error(AtlasError(f"Could not write batch artifacts: {exc}"), verbose=verbose)
            raise typer.Exit(1) from exc

    if json_output:
        console.print_json(summary.model_dump_json())
        if summary.failed:
            raise typer.Exit(1)
        return

    table = Table(
        title=Text(f"Batch {kind.value}", style=ATLAS_TITLE_STYLE),
        box=table_box(),
        header_style=ATLAS_MUTED_STYLE,
    )
    table.add_column("line", justify="right")
    table.add_column("status")
    table.add_column("kind")
    table.add_column("engine")
    table.add_column("url")
    table.add_column("message")
    for result in summary.results:
        table.add_row(
            str(result.entry.line_no),
            Text(result.status.value, style=_download_status_style(result.status)),
            _batch_result_plan_value(result.plan, "kind"),
            _batch_result_plan_value(result.plan, "engine"),
            result.entry.url,
            _batch_result_display_message(result),
        )
    console.print(table)
    console.print(_batch_summary_text(summary))
    if artifact_paths:
        _print_artifact_panel(artifact_paths)
    if summary.failed:
        raise typer.Exit(1)


def _batch_result_display_message(result: BatchItemResult) -> str:
    message = result.message or ""
    if result.status == DownloadStatus.success:
        lowered = message.lower()
        if "curl tls fallback" in lowered:
            return "Saved (curl fallback)"
        if lowered.startswith("saved to "):
            return "Saved"
    return message


def _run_saved_batch_session(
    session: Path | None,
    *,
    mode: str,
    output_dir: Path | None,
    backend: str,
    allow_sites: bool,
    allow_dirs: bool,
    concurrency: int | None,
    adaptive: bool,
    max_concurrency: int | None,
    per_host_concurrency: int | None,
    politeness: AdaptivePoliteness,
    dry_run: bool,
    json_output: bool,
    progress_mode: ProgressMode,
    verbose: bool,
) -> None:
    settings = _settings()
    base_output = output_dir or settings.output_dir
    session_path = _resolve_batch_session_path(session, output_dir=base_output)
    payload, manifest = _batch_session_payloads(session_path)
    kind = _batch_session_kind(payload, manifest)
    target_output = output_dir or _batch_session_output_dir(
        payload,
        manifest,
        session_path=session_path,
        fallback=settings.output_dir,
    )
    urls = _batch_session_urls_for_mode(payload, manifest, mode=mode)
    if not urls:
        _print_no_retry_urls(mode=mode, json_output=json_output)
        return
    if not json_output:
        console.print(
            Text.assemble(
                (f"{mode.replace('_', ' ')}: {len(urls)} URL(s) from ", ATLAS_MUTED_STYLE),
                (_display_path(str(session_path)), ATLAS_MUTED_STYLE),
            )
        )
    with _retry_batch_source(
        urls,
        output_dir=target_output,
        mode=mode,
        dry_run=dry_run,
    ) as retry_file:
        batch(
            retry_file,
            kind=kind,
            output_dir=target_output,
            backend=backend,
            allow_sites=allow_sites or kind == BatchKind.site,
            allow_dirs=allow_dirs or kind == BatchKind.dir,
            concurrency=concurrency,
            adaptive=adaptive,
            max_concurrency=max_concurrency,
            per_host_concurrency=per_host_concurrency,
            politeness=politeness,
            dry_run=dry_run,
            json_output=json_output,
            progress_mode=progress_mode,
            verbose=verbose,
        )


@app.command("retry")
def retry_command(
    session: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "retry.atlas.json, manifest.json, .atlas/latest directory, or output directory. "
                "Defaults to the configured output directory's latest session."
            ),
        ),
    ] = None,
    failed_only: Annotated[
        bool,
        typer.Option("--failed-only", help="Retry failed URLs. This is the default."),
    ] = False,
    checksum_failures_only: Annotated[
        bool,
        typer.Option("--checksum-failures-only", help="Retry only checksum failures."),
    ] = False,
    skipped_unknowns_only: Annotated[
        bool,
        typer.Option("--skipped-unknowns-only", help="Retry skipped unknown-route URLs."),
    ] = False,
    canceled_only: Annotated[
        bool,
        typer.Option("--canceled-only", help="Retry URLs canceled before item start."),
    ] = False,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    backend: Annotated[
        str,
        typer.Option("--backend", help="Backend override for file/site retry items."),
    ] = "auto",
    allow_sites: Annotated[
        bool,
        typer.Option("--allow-sites", help="Allow recursive site mirrors during retry."),
    ] = False,
    allow_dirs: Annotated[
        bool,
        typer.Option("--allow-dirs", help="Allow open-directory mirrors during retry."),
    ] = False,
    concurrency: Annotated[
        int | None,
        typer.Option("--concurrency", "-j", min=1, max=16, help="URLs to retry at once."),
    ] = None,
    adaptive: Annotated[
        bool,
        typer.Option("--adaptive", help="Re-scan retry URLs and tune concurrency."),
    ] = False,
    max_concurrency: Annotated[
        int | None,
        typer.Option("--max-concurrency", min=2, max=100, help="Adaptive global cap."),
    ] = None,
    per_host_concurrency: Annotated[
        int | None,
        typer.Option("--per-host-concurrency", min=1, max=100, help="Adaptive per-host cap."),
    ] = None,
    politeness: Annotated[
        AdaptivePoliteness,
        typer.Option("--politeness", help="Adaptive politeness profile."),
    ] = AdaptivePoliteness.normal,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print resolved retry plans only."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable summary.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Retry URLs from a saved Atlas session."""

    configure_logging(verbose)
    try:
        mode = _retry_mode_from_flags(
            failed_only=failed_only,
            checksum_failures_only=checksum_failures_only,
            skipped_unknowns_only=skipped_unknowns_only,
            canceled_only=canceled_only,
        )
        _run_saved_batch_session(
            session,
            mode=mode,
            output_dir=output_dir,
            backend=backend,
            allow_sites=allow_sites,
            allow_dirs=allow_dirs,
            concurrency=concurrency,
            adaptive=adaptive,
            max_concurrency=max_concurrency,
            per_host_concurrency=per_host_concurrency,
            politeness=politeness,
            dry_run=dry_run,
            json_output=json_output,
            progress_mode=progress_mode,
            verbose=verbose,
        )
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc


@app.command("resume")
def resume_command(
    session: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "retry.atlas.json, manifest.json, .atlas/latest directory, or output directory. "
                "Defaults to the configured output directory's latest session."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Download output directory.")
    ] = None,
    backend: Annotated[
        str,
        typer.Option("--backend", help="Backend override for file/site retry items."),
    ] = "auto",
    allow_sites: Annotated[
        bool,
        typer.Option("--allow-sites", help="Allow recursive site mirrors during resume."),
    ] = False,
    allow_dirs: Annotated[
        bool,
        typer.Option("--allow-dirs", help="Allow open-directory mirrors during resume."),
    ] = False,
    concurrency: Annotated[
        int | None,
        typer.Option("--concurrency", "-j", min=1, max=16, help="URLs to resume at once."),
    ] = None,
    adaptive: Annotated[
        bool,
        typer.Option("--adaptive", help="Re-scan resumed URLs and tune concurrency."),
    ] = False,
    max_concurrency: Annotated[
        int | None,
        typer.Option("--max-concurrency", min=2, max=100, help="Adaptive global cap."),
    ] = None,
    per_host_concurrency: Annotated[
        int | None,
        typer.Option("--per-host-concurrency", min=1, max=100, help="Adaptive per-host cap."),
    ] = None,
    politeness: Annotated[
        AdaptivePoliteness,
        typer.Option("--politeness", help="Adaptive politeness profile."),
    ] = AdaptivePoliteness.normal,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print resolved resume plans only."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable summary.")] = False,
    progress_mode: Annotated[
        ProgressMode,
        typer.Option("--progress", help="Progress display mode."),
    ] = ProgressMode.auto,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Resume a saved session by retrying failed, skipped-unknown, and canceled URLs."""

    configure_logging(verbose)
    try:
        _run_saved_batch_session(
            session,
            mode=BatchRetryMode.resume,
            output_dir=output_dir,
            backend=backend,
            allow_sites=allow_sites,
            allow_dirs=allow_dirs,
            concurrency=concurrency,
            adaptive=adaptive,
            max_concurrency=max_concurrency,
            per_host_concurrency=per_host_concurrency,
            politeness=politeness,
            dry_run=dry_run,
            json_output=json_output,
            progress_mode=progress_mode,
            verbose=verbose,
        )
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc


@app.command("export-failed")
def export_failed_command(
    session: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "retry.atlas.json, manifest.json, .atlas/latest directory, or output directory. "
                "Defaults to the configured output directory's latest session."
            ),
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write URLs to this file instead of stdout."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing export file."),
    ] = False,
    checksum_failures_only: Annotated[
        bool,
        typer.Option("--checksum-failures-only", help="Export only checksum failures."),
    ] = False,
    skipped_unknowns_only: Annotated[
        bool,
        typer.Option("--skipped-unknowns-only", help="Export skipped unknown-route URLs."),
    ] = False,
    canceled_only: Annotated[
        bool,
        typer.Option("--canceled-only", help="Export URLs canceled before item start."),
    ] = False,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--session-output-dir",
            help="Output directory used to find the latest session.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable report.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Export retryable URLs from a saved Atlas session."""

    configure_logging(verbose)
    try:
        settings = _settings()
        mode = _retry_mode_from_flags(
            failed_only=False,
            checksum_failures_only=checksum_failures_only,
            skipped_unknowns_only=skipped_unknowns_only,
            canceled_only=canceled_only,
        )
        base_output = output_dir or settings.output_dir
        session_path = _resolve_batch_session_path(session, output_dir=base_output)
        payload, manifest = _batch_session_payloads(session_path)
        urls = _batch_session_urls_for_mode(payload, manifest, mode=mode)
        if output is not None:
            _write_url_export(
                output,
                urls,
                protected_paths=_session_artifact_paths(session_path, payload, manifest),
                force=force,
            )
        if json_output:
            console.print_json(
                json.dumps(
                    {
                        "mode": mode,
                        "session": str(session_path),
                        "output": str(output.expanduser()) if output is not None else None,
                        "count": len(urls),
                        "urls": urls,
                    }
                )
            )
            return
        if output is not None:
            console.print(
                Text.assemble(
                    (
                        f"{status_glyph('success')} Exported {len(urls)} URL(s) to ",
                        ATLAS_SUCCESS_STYLE,
                    ),
                    (_display_path(str(output.expanduser())), ATLAS_SUCCESS_STYLE),
                )
            )
            return
        for url in urls:
            console.print(url)
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc


@app.command("inspect-session")
def inspect_session_command(
    session: Annotated[
        Path | None,
        typer.Argument(
            help=(
                "retry.atlas.json, manifest.json, .atlas/latest directory, or output directory. "
                "Defaults to the configured output directory's latest session."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--session-output-dir",
            help="Output directory used to find the latest session.",
        ),
    ] = None,
    preview: Annotated[
        SessionPreviewChoice,
        typer.Option("--preview", help="Show a bat-style preview pane."),
    ] = SessionPreviewChoice.none,
    panel: Annotated[
        SessionPanelChoice,
        typer.Option("--panel", help="Focus a lazygit-style saved-session panel."),
    ] = SessionPanelChoice.overview,
    item_line: Annotated[
        int | None,
        typer.Option("--item", min=1, help="Inspect one manifest item by line number."),
    ] = None,
    filter_text: Annotated[
        str | None,
        typer.Option(
            "--filter",
            "-f",
            help="Search item rows by URL, message, kind, engine, status, or line.",
        ),
    ] = None,
    status_filter: Annotated[
        SessionStatusFilter,
        typer.Option("--status", help="Filter item rows by status."),
    ] = SessionStatusFilter.all,
    kind_filter: Annotated[
        str | None,
        typer.Option("--kind-filter", help="Filter item rows by manifest kind."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=100, help="Rows to show in the active table."),
    ] = 8,
    copy_command: Annotated[
        SessionCommandChoice,
        typer.Option("--copy-command", help="Copy a saved-session operator command."),
    ] = SessionCommandChoice.none,
    open_output: Annotated[
        bool,
        typer.Option("--open-output", help="Open the session output folder in Finder."),
    ] = False,
    export_urls: Annotated[
        Path | None,
        typer.Option(
            "--export-urls",
            help="Write URLs from the current filtered item view to a text file.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace an existing URL export file."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable report.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detailed errors.")] = False,
) -> None:
    """Inspect a saved Atlas session without retrying or downloading."""

    configure_logging(verbose)
    try:
        settings = _settings()
        base_output = output_dir or settings.output_dir
        session_path = _resolve_batch_session_path(session, output_dir=base_output)
        payload, manifest = _batch_session_payloads(session_path)
        target_output = _batch_session_output_dir(
            payload,
            manifest,
            session_path=session_path,
            fallback=base_output,
        )
        report = _inspect_saved_session(
            session_path,
            payload,
            manifest,
            output_dir=target_output,
            item_line=item_line,
            limit=limit,
            filter_text=filter_text,
            status_filter=status_filter,
            kind_filter=kind_filter,
            panel=panel,
        )
        actions: dict[str, object] = {}
        opened_output = False
        copied = False
        copied_command = _session_command_for_choice(report, copy_command)
        if open_output:
            _open_saved_session_output(target_output)
            opened_output = True
            actions["opened_output"] = opened_output
        if export_urls is not None:
            exported_urls = _session_filtered_urls(
                manifest,
                filter_text=filter_text,
                status_filter=status_filter,
                kind_filter=kind_filter,
            )
            _write_url_export(
                export_urls,
                exported_urls,
                protected_paths=_session_artifact_paths(session_path, payload, manifest),
                force=force,
            )
            actions["exported_urls"] = str(export_urls.expanduser())
            actions["exported_count"] = len(exported_urls)
        if copied_command is not None:
            copied = _copy_text_to_clipboard(copied_command)
            actions["copied"] = copied
            actions["copied_command"] = copied_command
        elif copy_command != SessionCommandChoice.none:
            actions["copied"] = False
            actions["copied_command"] = None
            actions["copy_unavailable"] = copy_command.value
        if actions:
            report["actions"] = actions
        if json_output:
            console.print_json(json.dumps(report))
            return
        if copied_command is not None:
            status = "copied" if copied else "copy unavailable"
            console.print(
                Text(
                    f"Command {status}: {copied_command}",
                    style=ATLAS_MUTED_STYLE,
                )
            )
        elif copy_command != SessionCommandChoice.none:
            console.print(
                Text(
                    f"Command copy unavailable: {copy_command.value}",
                    style=ATLAS_MUTED_STYLE,
                )
            )
        if opened_output:
            console.print(
                Text(
                    f"Opened output folder: {_display_path(str(target_output))}",
                    style=ATLAS_MUTED_STYLE,
                )
            )
        if export_urls is not None:
            console.print(
                Text(
                    f"Exported {actions.get('exported_count', 0)} URL(s): "
                    f"{_display_path(str(export_urls.expanduser()))}",
                    style=ATLAS_MUTED_STYLE,
                )
            )
        _print_saved_session_inspection(
            report,
            preview=preview,
            panel=panel,
            payload=payload,
            manifest=manifest,
        )
    except AtlasError as exc:
        _handle_error(exc, verbose=verbose)
        raise typer.Exit(1) from exc


@app.command("setup")
def setup_command(
    full: Annotated[
        bool,
        typer.Option("--full", help="Install/check the full Atlas runtime."),
    ] = False,
    minimal: Annotated[
        bool,
        typer.Option("--minimal", help="Install/check Atlas media essentials."),
    ] = False,
    media_only: Annotated[
        bool,
        typer.Option("--media-only", help="Install/check only media runtime tools."),
    ] = False,
    mirrors: Annotated[
        bool,
        typer.Option("--mirrors", help="Install/check only website and directory mirror tools."),
    ] = False,
    install: Annotated[
        bool,
        typer.Option("--install", help="Run the proposed system package install commands."),
    ] = False,
    no_install: Annotated[
        bool,
        typer.Option("--no-install", help="Print the plan and create Atlas paths only."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Do not prompt before running install/update commands."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Machine-readable setup plan."),
    ] = False,
    open_menu: Annotated[
        bool,
        typer.Option("--open-menu", help="Open the interactive menu after successful setup."),
    ] = False,
) -> None:
    """Prepare Atlas paths and runtime prerequisites."""

    try:
        mode = _setup_mode_from_flags(
            full=full,
            minimal=minimal,
            media_only=media_only,
            mirrors=mirrors,
        )
        settings = _settings()
        plan = build_setup_plan(settings, mode=mode)
        if json_output:
            console.print_json(json.dumps(_setup_plan_as_dict(plan)))
            return
        _print_setup_plan(plan)
        install_requested = install and not no_install
        if install and no_install:
            raise AtlasError("Choose either --install or --no-install, not both.")
        if install_requested and not plan.can_install and plan.missing_tools:
            raise AtlasError(
                "Atlas cannot install missing tools automatically on this host. "
                "Use the printed manual commands."
            )
        if install_requested and plan.install_commands and not yes:
            install_requested = typer.confirm("Run these install commands?", default=True)
        result = apply_setup_plan(plan, settings, install=install_requested)
        _print_setup_result(result, install_requested=install_requested)
        if install_requested:
            console.print()
            console.print(
                f"[{ATLAS_TITLE_STYLE}]Verifying runtime with atlas doctor..."
                f"[/{ATLAS_TITLE_STYLE}]"
            )
            report = run_doctor(settings)
            if not report.ok:
                console.print(Text("Status: action required", style=ATLAS_ERROR_STYLE))
                raise typer.Exit(1)
            console.print(Text("Status: ready", style=ATLAS_SUCCESS_STYLE))
        elif plan.missing_tools:
            console.print()
            console.print(
                Text(
                    "Setup plan saved paths, but runtime tools are still missing. "
                    "Run again with --install --yes or use the printed commands.",
                    style=ATLAS_WARNING_STYLE,
                )
            )
        if open_menu:
            _launch_menu(force=True)
    except (AtlasError, subprocess.CalledProcessError, RuntimeError) as exc:
        _handle_error(exc, verbose=False)
        raise typer.Exit(1) from exc


@app.command("update")
def update_command(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the detected update command without running it."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Do not prompt before running the update command."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Machine-readable update plan."),
    ] = False,
) -> None:
    """Update Atlas using the detected install method."""

    try:
        plan = build_update_plan()
        if json_output:
            console.print_json(json.dumps(_update_plan_as_dict(plan)))
            return
        _print_update_plan(plan)
        if dry_run:
            return
        if not plan.can_update or plan.command is None:
            raise AtlasError(plan.detail)
        should_update = yes or typer.confirm("Run this update command?", default=True)
        if should_update:
            run_update_plan(plan)
    except (AtlasError, subprocess.CalledProcessError, RuntimeError) as exc:
        _handle_error(exc, verbose=False)
        raise typer.Exit(1) from exc


@app.command()
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable report.")] = False,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Plan or run setup repairs for missing runtime tools."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Do not prompt before running fix commands."),
    ] = False,
    no_install: Annotated[
        bool,
        typer.Option("--no-install", help="With --fix, print repair commands but do not run them."),
    ] = False,
    network: Annotated[
        bool,
        typer.Option("--network", help="Show only Python SSL, CA, and HTTPS diagnostics."),
    ] = False,
    fix_certs: Annotated[
        bool,
        typer.Option("--fix-certs", help="Show safe certificate repair guidance."),
    ] = False,
) -> None:
    """Check runtime dependencies and writable paths."""

    settings = _settings()
    report = run_doctor(settings)
    if network:
        report = _network_doctor_report(report)
    network_failed = network and any(not check.ok for check in report.checks)
    if json_output and fix:
        plan = build_setup_plan(settings, mode=SetupMode.full)
        console.print_json(
            json.dumps(
                {
                    "doctor": report.model_dump(mode="json"),
                    "setup_plan": _setup_plan_as_dict(plan),
                }
            )
        )
        if not report.ok:
            raise typer.Exit(1)
        return
    if json_output:
        payload: dict[str, object] = {"doctor": report.model_dump(mode="json")}
        if fix_certs:
            payload["certificate_repair"] = _certificate_repair_plan(report)
        output = payload if fix_certs or network else report.model_dump(mode="json")
        console.print_json(json.dumps(output))
        if not report.ok or network_failed:
            raise typer.Exit(1)
        return

    console.print(f"[{ATLAS_TITLE_STYLE}]atlas doctor[/{ATLAS_TITLE_STYLE}]\n")
    for check in report.checks:
        if check.ok:
            marker = Text(status_glyph("success"), style=ATLAS_SUCCESS_STYLE)
        elif check.required:
            marker = Text(status_glyph("error"), style=ATLAS_ERROR_STYLE)
        else:
            marker = Text(status_glyph("optional"), style=ATLAS_MUTED_STYLE)
        name = _doctor_label(check.name)
        detail = check.detail
        if " dir" in check.name or check.name == "output dir":
            detail = _display_path(detail)
        line = Text.assemble(
            marker,
            " ",
            (f"{name:<22}", ATLAS_ACTIVE_STYLE),
            (detail, ATLAS_MUTED_STYLE if not check.ok and not check.required else ""),
        )
        console.print(line)
    console.print()
    if report.ok and not network_failed:
        console.print(Text("Status: ready", style=ATLAS_SUCCESS_STYLE))
    else:
        console.print(Text("Status: action required", style=ATLAS_ERROR_STYLE))
        missing_required = [check for check in report.checks if check.required and not check.ok]
        for check in missing_required:
            if check.hint:
                console.print()
                error_marker = status_glyph("error")
                console.print(
                    Text(
                        f"{error_marker} {check.name} is required.",
                        style=ATLAS_ERROR_STYLE,
                    )
                )
                console.print("Install it with:")
                console.print(
                    f"[{ATLAS_ACTIVE_STYLE}]{escape(_install_command(check.name))}"
                    f"[/{ATLAS_ACTIVE_STYLE}]"
                )
    if fix_certs:
        _print_certificate_repair_plan(report)
    if fix:
        console.print()
        plan = build_setup_plan(settings, mode=SetupMode.full)
        _print_setup_plan(plan)
        install_requested = not no_install and plan.can_install and bool(plan.missing_tools)
        if install_requested and not yes:
            install_requested = typer.confirm("Run these install commands?", default=True)
        result = apply_setup_plan(plan, settings, install=install_requested)
        _print_setup_result(result, install_requested=install_requested)
        if install_requested:
            report = run_doctor(settings)
    if not report.ok or network_failed:
        raise typer.Exit(1)


_NETWORK_DOCTOR_CHECKS = {
    "Python",
    "Python SSL",
    "CA bundle",
    "HTTPS verification",
}


def _network_doctor_report(report: DoctorReport) -> DoctorReport:
    return DoctorReport(
        checks=[check for check in report.checks if check.name in _NETWORK_DOCTOR_CHECKS]
    )


def _certificate_repair_plan(report: DoctorReport) -> dict[str, object]:
    checks = {check.name: check for check in report.checks}
    ca = checks.get("CA bundle")
    https = checks.get("HTTPS verification")
    return {
        "safe": True,
        "mutates_system": False,
        "ca_bundle": ca.detail if ca else None,
        "https_verification": https.detail if https else None,
        "steps": [
            "Run atlas doctor --network to verify Python TLS and CA bundle state.",
            "Run atlas setup --minimal to repair Atlas runtime dependencies.",
            "If installed with Homebrew, run "
            "brew reinstall ca-certificates openssl@3 python@3.12.",
            "Retry the scan before considering any no-check-certificate backend option.",
        ],
    }


def _print_certificate_repair_plan(report: DoctorReport) -> None:
    plan = _certificate_repair_plan(report)
    console.print()
    console.print(
        f"[{ATLAS_TITLE_STYLE}]Certificate repair[/{ATLAS_TITLE_STYLE}]"
    )
    console.print("Atlas will not disable TLS verification or mutate system trust silently.")
    ca_bundle = plan.get("ca_bundle")
    if ca_bundle:
        console.print(
            f"[{ATLAS_MUTED_STYLE}]CA bundle:[/{ATLAS_MUTED_STYLE}] "
            f"{escape(str(ca_bundle))}"
        )
    https_verification = plan.get("https_verification")
    if https_verification:
        console.print(
            f"[{ATLAS_MUTED_STYLE}]HTTPS verification:[/{ATLAS_MUTED_STYLE}] "
            f"{escape(str(https_verification))}"
        )
    console.print()
    steps = plan.get("steps", ())
    if isinstance(steps, list):
        for step in steps:
            console.print(f"{status_glyph('transition')} {escape(str(step))}")


@config_app.command("path")
def config_path_command() -> None:
    """Print the config file path."""

    console.print(_display_path(config_path()))


@config_app.command("show")
def config_show() -> None:
    """Show the resolved configuration."""

    settings = _settings()
    console.print(settings_as_toml(settings))


def _doctor_label(name: str) -> str:
    labels = {
        "atlas package": "atlas",
        "config dir": "Config dir",
        "data dir": "Data dir",
        "cache dir": "Cache dir",
        "log dir": "Log dir",
        "output dir": "Output dir",
        "browser cookie support": "Cookies",
    }
    return labels.get(name, name)


def _install_command(name: str) -> str:
    if name in {"ffmpeg", "ffprobe"}:
        return "brew install ffmpeg"
    if name == "aria2c":
        return "brew install aria2"
    if name == "wget2":
        return "brew install wget2"
    if name == "wget":
        return "brew install wget"
    if name == "yt-dlp":
        return "uv tool install --force ."
    if name == "atlas package":
        return "brew install xkam7ar/tap/atlas"
    return "atlas doctor"


def _setup_mode_from_flags(
    *,
    full: bool,
    minimal: bool,
    media_only: bool,
    mirrors: bool,
) -> SetupMode:
    selected = [
        mode
        for enabled, mode in (
            (full, SetupMode.full),
            (minimal, SetupMode.minimal),
            (media_only, SetupMode.media_only),
            (mirrors, SetupMode.mirrors),
        )
        if enabled
    ]
    if len(selected) > 1:
        raise AtlasError(
            "Choose only one setup mode: --full, --minimal, --media-only, or --mirrors."
        )
    return selected[0] if selected else SetupMode.full


def _setup_plan_as_dict(plan: SetupPlan) -> dict[str, object]:
    return {
        "mode": plan.mode.value,
        "environment": {
            "os": plan.environment.os_name,
            "architecture": plan.environment.architecture,
            "shell": plan.environment.shell,
            "package_manager": plan.environment.package_manager,
            "package_manager_path": plan.environment.package_manager_path,
            "install_method": plan.environment.install_method,
            "atlas_executable": plan.environment.atlas_executable,
        },
        "tools": [
            {
                "executable": tool.executable,
                "package": tool.package,
                "purpose": tool.purpose,
                "required": tool.required,
                "installed": tool in plan.existing_tools,
            }
            for tool in plan.tools
        ],
        "missing_tools": [tool.executable for tool in plan.missing_tools],
        "install_commands": [
            " ".join(shlex.quote(part) for part in command)
            for command in plan.install_commands
        ],
        "manual_commands": list(plan.manual_commands),
        "config_file": str(plan.config_file),
        "output_dir": str(plan.output_dir),
        "can_install": plan.can_install,
        "complete": plan.complete,
        "notes": list(plan.notes),
    }


def _update_plan_as_dict(plan: UpdatePlan) -> dict[str, object]:
    return {
        "install_method": plan.install_method,
        "command": list(plan.command) if plan.command else None,
        "detail": plan.detail,
        "can_update": plan.can_update,
    }


def _print_setup_plan(plan: SetupPlan) -> None:
    header = Table.grid(padding=(0, 2))
    header.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    header.add_column()
    header.add_row("Mode", plan.mode.value)
    header.add_row("OS", f"{plan.environment.os_name} {plan.environment.architecture}")
    header.add_row("Package manager", plan.environment.package_manager or "not detected")
    header.add_row("Install method", plan.environment.install_method)
    header.add_row("Config", _display_path(plan.config_file))
    header.add_row("Output", _display_path(plan.output_dir))
    console.print(
        Panel(
            header,
            title=Text("atlas Setup", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            expand=False,
        )
    )

    table = Table(
        title=Text("Runtime tools", style=ATLAS_TITLE_STYLE),
        box=table_box(),
        header_style=ATLAS_MUTED_STYLE,
    )
    table.add_column("Status", no_wrap=True)
    table.add_column("Tool", no_wrap=True)
    table.add_column("Package", no_wrap=True)
    table.add_column("Purpose")
    for tool in plan.tools:
        installed = tool in plan.existing_tools
        marker = status_glyph("success") if installed else status_glyph("optional")
        style = (
            ATLAS_SUCCESS_STYLE
            if installed
            else (ATLAS_WARNING_STYLE if tool.required else ATLAS_MUTED_STYLE)
        )
        table.add_row(marker, tool.executable, tool.package, tool.purpose, style=style)
    console.print(table)

    if plan.install_commands:
        console.print(
            f"[{ATLAS_TITLE_STYLE}]Will install missing tools with:"
            f"[/{ATLAS_TITLE_STYLE}]"
        )
        for command in plan.install_commands:
            console.print("  " + " ".join(shlex.quote(part) for part in command))
    elif plan.manual_commands:
        console.print(
            f"[{ATLAS_WARNING_STYLE}]Install missing tools manually:"
            f"[/{ATLAS_WARNING_STYLE}]"
        )
        for manual_command in plan.manual_commands:
            console.print(f"  {escape(manual_command)}")
    else:
        console.print(
            Text("All selected runtime tools are installed.", style=ATLAS_SUCCESS_STYLE)
        )
    for note in plan.notes:
        console.print(Text(f"! {note}", style=ATLAS_WARNING_STYLE))


def _print_setup_result(result: SetupResult, *, install_requested: bool) -> None:
    console.print()
    console.print(Text("Setup paths ready", style=ATLAS_SUCCESS_STYLE))
    if result.config_written:
        console.print(f"  Wrote config: {_styled_path(result.created_paths[0] / 'config.toml')}")
    if install_requested:
        if result.commands_run:
            for command in result.commands_run:
                console.print("  Ran: " + " ".join(shlex.quote(part) for part in command))
        else:
            console.print("  No install commands were needed.")


def _print_update_plan(plan: UpdatePlan) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    grid.add_column()
    grid.add_row("Install method", plan.install_method)
    grid.add_row("Status", plan.detail)
    if plan.command is not None:
        grid.add_row("Command", " ".join(shlex.quote(part) for part in plan.command))
    else:
        grid.add_row("Command", "not available")
    console.print(
        Panel(
            grid,
            title=Text("atlas Update", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            expand=False,
        )
    )
