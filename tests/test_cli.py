from __future__ import annotations

import json
import shutil
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text
from rich.theme import Theme
from typer.testing import CliRunner

import atlas.cli as cli
from atlas.cli import (
    _print_hub_plan,
    _print_site_summary,
    _run_batch_hub_plan,
    _write_batch_artifacts,
    _write_mirror_artifacts,
    app,
)
from atlas.config import AtlasSettings
from atlas.errors import AtlasError, ConfigError
from atlas.models import (
    AdaptiveDownloadPlan,
    AdaptivePoliteness,
    AudioDownloadOptions,
    BatchEntry,
    BatchItemResult,
    BatchKind,
    BatchSummary,
    DirectFileProbe,
    DoctorCheck,
    DoctorReport,
    DownloadPlan,
    DownloadResult,
    DownloadStatus,
    EngineKind,
    EngineRoute,
    FileDownloadOptions,
    FileSizeClass,
    FormatInfo,
    HubKind,
    InfoOptions,
    MediaInfo,
    OptimizedDownloadPlan,
    OrganizeMode,
    ProgressEvent,
    ProgressPhase,
    ScanStatus,
    SiteDownloadOptions,
    VideoCodecChoice,
    VideoDownloadOptions,
    WorkBucket,
    WorkItem,
)
from atlas.optimizer import HubExecutionPlan
from atlas.runner import ProcessControl
from atlas.sessions import SmartDownloadSession
from atlas.setup import (
    PackageManager,
    RuntimeTool,
    SetupEnvironment,
    SetupMode,
    SetupPlan,
    UpdatePlan,
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
    resolve_theme,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    monkeypatch.setattr("atlas.cli.load_config", lambda: settings)
    monkeypatch.setattr("atlas.preflight.which", lambda name: f"/opt/bin/{name}")


def test_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "menu" in result.output
    assert "video" in result.output
    assert "audio" in result.output
    assert "get" in result.output
    assert "file" in result.output
    assert "site" in result.output
    assert "dir" in result.output
    assert "playlist" in result.output
    assert "retry" in result.output
    assert "resume" in result.output
    assert "export-failed" in result.output
    assert "inspect-session" in result.output
    assert "setup" in result.output
    assert "update" in result.output
    assert "doctor" in result.output
    assert "ytdlp" in result.output
    assert "aria2" in result.output
    assert "wget" in result.output
    assert "--theme" in result.output
    assert "--version" in result.output
    assert "--plain" in result.output
    assert "--no-unicode" in result.output
    assert "--no-animation" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output == "atlas 0.1.0\n"


def test_site_summary_handles_unset_optional_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=output, width=100, theme=Theme(resolve_theme(AtlasThemeName.light))),
    )
    options = SiteDownloadOptions(
        url="https://example.com/",
        output_dir=tmp_path,
    )

    _print_site_summary(options, "wget2")

    assert "Site Mirror" in output.getvalue()
    assert "max-runtime" not in output.getvalue()


def test_plain_help_and_parse_errors_are_ascii() -> None:
    help_result = runner.invoke(app, ["--plain", "--help"])
    no_unicode_help_result = runner.invoke(app, ["--no-unicode", "--help"])
    dumb_help_result = runner.invoke(app, ["--help"], env={"TERM": "dumb"})
    error_result = runner.invoke(app, ["--plain", "not-a-command"])

    assert help_result.exit_code == 0
    assert no_unicode_help_result.exit_code == 0
    assert dumb_help_result.exit_code == 0
    assert error_result.exit_code == 2
    for output in (
        help_result.output,
        no_unicode_help_result.output,
        dumb_help_result.output,
        error_result.output,
    ):
        assert "\x1b" not in output
        assert output.isascii()


def test_no_args_non_tty_shows_help() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Usage: atlas" in result.output
    assert "menu" in result.output


def test_cli_menu_action_preserves_download_error_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(output_dir=tmp_path)
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
    )
    route = EngineRoute(
        kind=HubKind.file,
        engine=EngineKind.native,
        reason="test",
        url=options.url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(route=route, output=tmp_path, summary={}),
        options=options,
    )

    def fail_execution(_settings: AtlasSettings, _plan: HubExecutionPlan) -> list[Path]:
        try:
            raise AtlasError("backend unavailable")
        except AtlasError as exc:
            raise cli.typer.Exit(1) from exc

    monkeypatch.setattr(cli, "_execute_hub_plan", fail_execution)

    with pytest.raises(AtlasError, match="backend unavailable"):
        cli._CliMenuActions(settings).execute_plan(plan)


def test_no_args_tty_launches_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "atlas.cli._should_auto_launch_menu",
        lambda *, no_menu, json_output: True,
    )
    monkeypatch.setattr("atlas.cli._launch_menu", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert calls == [{}]


def test_no_args_json_suppresses_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli._launch_menu",
        lambda **_kwargs: pytest.fail("menu should not open"),
    )

    result = runner.invoke(app, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "help"
    assert payload["name"] == "atlas"
    assert "Usage: atlas" in payload["help"]


@pytest.mark.parametrize(
    "args",
    [
        ["get", "--json"],
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "bogus",
            "--dry-run",
            "--json",
        ],
        ["file", "https://example.com/archive.zip", "--unknown-option", "--json"],
    ],
)
def test_parser_errors_are_single_json_documents(args: list[str]) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "Usage:" not in result.output


def test_parser_error_is_one_terminal_ndjson_event() -> None:
    result = runner.invoke(app, ["get", "--progress", "json"])

    assert result.exit_code == 2
    lines = [json.loads(line) for line in result.output.splitlines() if line]
    assert len(lines) == 1
    assert lines[0]["status"] == "error"
    assert lines[0]["exit_code"] == 2


def test_menu_command_forces_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("atlas.cli._launch_menu", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(app, ["menu"])

    assert result.exit_code == 0
    assert calls == [{"force": True}]


def test_config_path() -> None:
    result = runner.invoke(app, ["config", "path"])

    assert result.exit_code == 0
    assert "atlas" in result.output


def test_config_show() -> None:
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0
    assert "default_output_dir" in result.output
    assert "use_aria2 = true" in result.output


def test_doctor_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_doctor",
        lambda _settings, **_kwargs: DoctorReport(
            checks=[DoctorCheck(name="Python", ok=True, detail="3.12.7")]
        ),
    )

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert '"name": "Python"' in result.output
    assert '"ok": true' in result.output


def test_doctor_json_config_error_is_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_config() -> AtlasSettings:
        raise ConfigError("Invalid TOML: secret_token=TOPSECRET")

    monkeypatch.setattr("atlas.cli.load_config", fail_config)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert payload["error"]["type"] == "ConfigError"
    assert "TOPSECRET" not in result.output


def test_doctor_network_json_filters_network_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_doctor",
        lambda _settings, **_kwargs: DoctorReport(
            checks=[
                DoctorCheck(name="Python", ok=True, detail="3.12.7"),
                DoctorCheck(name="Python SSL", ok=True, detail="OpenSSL"),
                DoctorCheck(name="CA bundle", ok=True, detail="/certifi.pem", required=False),
                DoctorCheck(name="HTTPS verification", ok=True, detail="verified", required=False),
                DoctorCheck(name="ffmpeg", ok=False, detail="not found"),
            ]
        ),
    )

    result = runner.invoke(app, ["doctor", "--network", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    names = [check["name"] for check in payload["doctor"]["checks"]]
    assert names == ["Python", "Python SSL", "CA bundle", "HTTPS verification"]


def test_doctor_network_json_fails_on_https_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_doctor",
        lambda _settings, **_kwargs: DoctorReport(
            checks=[
                DoctorCheck(name="Python", ok=True, detail="3.12.7"),
                DoctorCheck(name="Python SSL", ok=True, detail="OpenSSL"),
                DoctorCheck(name="CA bundle", ok=True, detail="/certifi.pem", required=False),
                DoctorCheck(
                    name="HTTPS verification",
                    ok=False,
                    detail="TLS certificate verification failed",
                    required=False,
                ),
            ]
        ),
    )

    result = runner.invoke(app, ["doctor", "--network", "--json"])

    assert result.exit_code == 1
    assert "TLS certificate verification failed" in result.output


def test_doctor_fix_certs_outputs_safe_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_doctor",
        lambda _settings, **_kwargs: DoctorReport(
            checks=[
                DoctorCheck(name="Python", ok=True, detail="3.12.7"),
                DoctorCheck(name="Python SSL", ok=True, detail="OpenSSL"),
                DoctorCheck(name="CA bundle", ok=True, detail="/certifi.pem", required=False),
                DoctorCheck(
                    name="HTTPS verification",
                    ok=False,
                    detail="TLS certificate verification failed",
                    required=False,
                ),
            ]
        ),
    )

    result = runner.invoke(app, ["doctor", "--network", "--fix-certs"])

    assert result.exit_code == 1
    assert "Certificate repair" in result.output
    assert "will not disable TLS verification" in result.output
    assert "atlas setup --minimal" in result.output


def _fake_setup_plan(tmp_path: Path) -> SetupPlan:
    tool = RuntimeTool(
        executable="ffmpeg",
        packages=dict.fromkeys(PackageManager, "ffmpeg"),
        purpose="media runtime",
        modes=frozenset({SetupMode.full}),
        required=True,
    )
    return SetupPlan(
        mode=SetupMode.full,
        environment=SetupEnvironment(
            os_name="macOS",
            architecture="arm64",
            shell="zsh",
            package_manager="homebrew",
            package_manager_path="/opt/homebrew/bin/brew",
            install_method="homebrew",
            atlas_executable="/opt/homebrew/bin/atlas",
        ),
        tools=(tool,),
        missing_tools=(tool,),
        existing_tools=(),
        install_commands=(("brew", "install", "ffmpeg"),),
        manual_commands=("brew install ffmpeg",),
        config_file=tmp_path / "config.toml",
        output_dir=tmp_path / "out",
        can_install=True,
    )


def test_setup_json_outputs_install_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.cli.build_setup_plan",
        lambda _settings, *, mode: _fake_setup_plan(tmp_path),
    )

    result = runner.invoke(app, ["setup", "--full", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "full"
    assert payload["environment"]["package_manager"] == "homebrew"
    assert payload["missing_tools"] == ["ffmpeg"]
    assert payload["install_commands"] == ["brew install ffmpeg"]


@pytest.mark.parametrize(
    ("manager", "package"),
    [
        (PackageManager.homebrew, "ffmpeg"),
        (PackageManager.apt, "ffmpeg"),
        (PackageManager.dnf, "ffmpeg-free"),
        (PackageManager.pacman, "ffmpeg"),
    ],
)
def test_setup_json_reports_host_specific_package(
    tmp_path: Path,
    manager: PackageManager,
    package: str,
) -> None:
    tool = RuntimeTool(
        executable="ffmpeg",
        packages={
            PackageManager.homebrew: "ffmpeg",
            PackageManager.apt: "ffmpeg",
            PackageManager.dnf: "ffmpeg-free",
            PackageManager.pacman: "ffmpeg",
        },
        purpose="media runtime",
        modes=frozenset({SetupMode.full}),
        required=True,
    )
    plan = SetupPlan(
        mode=SetupMode.full,
        environment=SetupEnvironment(
            os_name="macOS" if manager == PackageManager.homebrew else "Linux",
            architecture="x86_64",
            shell="bash",
            package_manager=manager,
            package_manager_path=f"/usr/bin/{manager.value}",
            install_method="unknown",
            atlas_executable=None,
        ),
        tools=(tool,),
        missing_tools=(tool,),
        existing_tools=(),
        install_commands=(),
        manual_commands=(),
        config_file=tmp_path / "config.toml",
        output_dir=tmp_path / "out",
        can_install=False,
    )

    payload = cli._setup_plan_as_dict(plan)

    assert payload["environment"]["package_manager"] == manager.value  # type: ignore[index]
    assert payload["tools"][0]["package"] == package  # type: ignore[index]


def test_setup_no_install_is_plan_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.cli.build_setup_plan",
        lambda _settings, *, mode: _fake_setup_plan(tmp_path),
    )
    monkeypatch.setattr(
        "atlas.cli.apply_setup_plan",
        lambda *_args, **_kwargs: pytest.fail("plan-only setup must not mutate paths"),
    )

    result = runner.invoke(app, ["setup", "--full", "--no-install"])

    assert result.exit_code == 0
    assert "Will install missing tools with" in result.output
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "out").exists()


def test_doctor_fix_no_install_is_plan_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_doctor",
        lambda _settings, **_kwargs: DoctorReport(
            checks=[DoctorCheck(name="Python", ok=True, detail="3.12.7")]
        ),
    )
    monkeypatch.setattr(
        "atlas.cli.build_setup_plan",
        lambda _settings, *, mode: _fake_setup_plan(tmp_path),
    )
    monkeypatch.setattr(
        "atlas.cli.apply_setup_plan",
        lambda *_args, **_kwargs: pytest.fail("plan-only doctor must not mutate paths"),
    )

    result = runner.invoke(app, ["doctor", "--fix", "--no-install"])

    assert result.exit_code == 0
    assert "Will install missing tools with" in result.output
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "out").exists()


def test_setup_rejects_conflicting_modes() -> None:
    result = runner.invoke(app, ["setup", "--full", "--mirrors", "--json"])

    assert result.exit_code == 1
    assert "Choose only one setup mode" in result.output


def test_update_json_outputs_detected_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli.build_update_plan",
        lambda: UpdatePlan(
            install_method="homebrew",
            command=("brew", "upgrade", "xkam7ar/tap/atlas"),
            detail="Atlas appears to be installed through Homebrew.",
            can_update=True,
        ),
    )

    result = runner.invoke(app, ["update", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["install_method"] == "homebrew"
    assert payload["command"] == ["brew", "upgrade", "xkam7ar/tap/atlas"]


def test_update_passes_explicit_release_ref_to_uv_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str | None] = []

    def fake_plan(*, release_ref: str | None = None) -> UpdatePlan:
        captured.append(release_ref)
        return UpdatePlan(
            install_method="uv-tool",
            command=("uv", "tool", "install", "--force", f"git+repo@{release_ref}"),
            detail="Pinned uv-tool update.",
            can_update=True,
        )

    monkeypatch.setattr("atlas.cli.build_update_plan", fake_plan)

    release_ref = "a" * 40
    result = runner.invoke(app, ["update", "--release-ref", release_ref, "--json"])

    assert result.exit_code == 0
    assert captured == [release_ref]
    assert json.loads(result.output)["command"][-1] == f"git+repo@{release_ref}"


def test_setup_and_update_panels_follow_selected_theme_styles(tmp_path: Path) -> None:
    output = StringIO()
    previous_console = cli.console
    try:
        configure_visuals(theme=AtlasThemeName.light, color=True, unicode=True, env={})
        cli.console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            no_color=False,
            theme=Theme(resolve_theme(AtlasThemeName.light)),
        )

        cli._print_setup_plan(_fake_setup_plan(tmp_path))
        cli._print_update_plan(
            UpdatePlan(
                install_method="homebrew",
                command=("brew", "upgrade", "xkam7ar/tap/atlas"),
                detail="Atlas appears to be installed through Homebrew.",
                can_update=True,
            )
        )

        rendered = output.getvalue()
        assert "\x1b[1;34m atlas Setup " in rendered
        assert "\x1b[1;34m atlas Update " in rendered
        assert "\x1b[1;36m atlas Setup " not in rendered
        assert "\x1b[1;36m atlas Update " not in rendered
    finally:
        cli.console = previous_console
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_doctor_fix_json_includes_setup_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_doctor",
        lambda _settings, **_kwargs: DoctorReport(
            checks=[DoctorCheck(name="Python", ok=True, detail="3.12.7")]
        ),
    )
    monkeypatch.setattr(
        "atlas.cli.build_setup_plan",
        lambda _settings, *, mode: _fake_setup_plan(tmp_path),
    )

    result = runner.invoke(app, ["doctor", "--json", "--fix"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["doctor"]["checks"][0]["name"] == "Python"
    assert payload["setup_plan"]["missing_tools"] == ["ffmpeg"]


def test_doctor_fix_network_json_fails_on_https_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_doctor",
        lambda _settings, **_kwargs: DoctorReport(
            checks=[
                DoctorCheck(name="Python", ok=True, detail="3.12.7"),
                DoctorCheck(
                    name="HTTPS verification",
                    ok=False,
                    detail="TLS certificate verification failed",
                    required=False,
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "atlas.cli.build_setup_plan",
        lambda _settings, *, mode: _fake_setup_plan(tmp_path),
    )

    result = runner.invoke(app, ["doctor", "--network", "--json", "--fix"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["doctor"]["checks"][1]["name"] == "HTTPS verification"
    assert payload["setup_plan"]["missing_tools"] == ["ffmpeg"]


def test_video_dry_run() -> None:
    result = runner.invoke(app, ["video", "https://example.com/watch?v=1", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "bestvideo*+bestaudio/best" in result.output


def test_video_dry_run_applies_selected_codec() -> None:
    result = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--video-codec",
            "vp9",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "[vcodec^=vp9]" in result.output


def test_video_dry_run_json_redacts_credentialed_proxy() -> None:
    result = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--proxy",
            "http://alice:sentinel-secret@proxy.example:8080",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "sentinel-secret" not in result.output
    assert '"proxy": "<redacted>"' in result.output


def test_video_dry_run_applies_advanced_media_options() -> None:
    result = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--concurrent-fragments",
            "8",
            "--file-access-retries",
            "5",
            "--retry-sleep",
            "http:1",
            "--abort-unavailable-fragments",
            "--throttled-rate",
            "64K",
            "--http-chunk-size",
            "10M",
            "--socket-timeout",
            "12",
            "--source-address",
            "127.0.0.1",
            "--impersonate",
            "chrome",
            "--extractor-args",
            "youtube:player_client=android",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"concurrent_fragment_downloads": 8' in result.output
    assert '"file_access_retries": 5' in result.output
    assert '"skip_unavailable_fragments": false' in result.output
    assert '"throttledratelimit": 65536' in result.output
    assert '"http_chunk_size": 10485760' in result.output
    assert '"socket_timeout": 12.0' in result.output
    assert '"source_address": "127.0.0.1"' in result.output
    assert '"impersonate": "chrome"' in result.output
    assert '"player_client": [' in result.output
    assert '"retry_sleep_functions": {' in result.output


def test_video_dry_run_applies_selection_sections_and_sponsorblock() -> None:
    result = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--match-filter",
            "duration>?60",
            "--break-match-filter",
            "view_count<10",
            "--max-downloads",
            "2",
            "--break-on-existing",
            "--break-on-reject",
            "--break-per-input",
            "--date-after",
            "20240101",
            "--date-before",
            "20240601",
            "--min-filesize",
            "10M",
            "--max-filesize",
            "1G",
            "--reject-live",
            "--reject-upcoming",
            "--live-from-start",
            "--download-section",
            "intro",
            "--sponsorblock-mark",
            "sponsor",
            "--sponsorblock-remove",
            "selfpromo",
            "--sponsorblock-api",
            "https://sb.example",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"match_filter": "<callable>"' in result.output
    assert '"download_ranges": "<callable>"' in result.output
    assert '"max_downloads": 2' in result.output
    assert '"break_on_existing": true' in result.output
    assert '"break_on_reject": true' in result.output
    assert '"break_per_url": true' in result.output
    assert '"min_filesize": 10485760' in result.output
    assert '"max_filesize": 1073741824' in result.output
    assert '"live_from_start": true' in result.output
    assert '"key": "SponsorBlock"' in result.output
    assert '"api": "https://sb.example"' in result.output
    assert '"remove_sponsor_segments": [' in result.output


def test_audio_dry_run() -> None:
    result = runner.invoke(app, ["audio", "https://example.com/watch?v=1", "--dry-run"])

    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "FFmpegExtractAudio" in result.output


def test_audio_dry_run_applies_selected_codec() -> None:
    result = runner.invoke(
        app,
        [
            "audio",
            "https://example.com/watch?v=1",
            "--codec",
            "mp3",
            "--quality",
            "3",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"preferredcodec": "mp3"' in result.output
    assert '"preferredquality": "3"' in result.output


def test_video_explicit_playlist_requires_playlist_intent() -> None:
    result = runner.invoke(
        app,
        [
            "video",
            "https://www.youtube.com/playlist?list=PL123",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "explicit playlist URL" in result.output
    assert "Traceback" not in result.output


def test_video_explicit_playlist_can_be_accepted() -> None:
    result = runner.invoke(
        app,
        [
            "video",
            "https://www.youtube.com/playlist?list=PL123",
            "--playlist",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"noplaylist": false' in result.output
    assert '"ignoreerrors": "only_download"' in result.output


def test_media_sidecar_only_flags_dry_run() -> None:
    subtitles = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--subtitle-only",
            "--sub-lang",
            "en",
            "--dry-run",
            "--json",
        ],
    )
    thumbnail = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--thumbnail-only",
            "--dry-run",
            "--json",
        ],
    )
    info = runner.invoke(
        app,
        [
            "audio",
            "https://example.com/watch?v=1",
            "--info-only",
            "--dry-run",
            "--json",
        ],
    )

    assert subtitles.exit_code == 0
    assert '"skip_download": true' in subtitles.output
    assert '"writesubtitles": true' in subtitles.output
    assert '"subtitleslangs": [' in subtitles.output
    assert thumbnail.exit_code == 0
    assert '"skip_download": true' in thumbnail.output
    assert '"writethumbnail": true' in thumbnail.output
    assert info.exit_code == 0
    assert '"skip_download": true' in info.output
    assert '"writeinfojson": true' in info.output
    assert "FFmpegExtractAudio" not in info.output


def test_high_level_commands_use_hub_optimizer(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlas.optimizer import DownloadOptimizer

    calls: list[str] = []
    original = DownloadOptimizer.optimize_options

    def spy(self, route, options):
        calls.append(route.kind.value)
        return original(self, route, options)

    monkeypatch.setattr(DownloadOptimizer, "optimize_options", spy)

    commands = [
        ["video", "https://example.com/watch?v=1", "--dry-run"],
        ["audio", "https://example.com/watch?v=1", "--dry-run"],
        ["file", "https://example.com/archive.zip", "--backend", "native", "--dry-run"],
        ["site", "https://example.com/docs/", "--backend", "wget2", "--dry-run"],
        ["dir", "https://example.com/files/", "--backend", "wget2", "--dry-run"],
    ]

    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0

    assert calls == ["video", "audio", "file", "site", "dir"]


def test_batch_items_use_hub_optimizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.optimizer import DownloadOptimizer

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\nhttps://two.example\n", encoding="utf-8")
    calls: list[str] = []
    original = DownloadOptimizer.optimize_options

    def spy(self, route, options):
        calls.append(route.kind.value)
        return original(self, route, options)

    monkeypatch.setattr(DownloadOptimizer, "optimize_options", spy)

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--type", "video", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert calls == ["video", "video"]


def test_file_dry_run_json() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "native",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"backend": "native"' in result.output
    assert "archive.zip" in result.output


def test_persisted_aria2_defaults_apply_to_file_and_media_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AtlasSettings(
        output_dir=tmp_path,
        archive_file=tmp_path / "archive.txt",
        aria2_connections=7,
        aria2_splits=6,
        aria2_chunk_size="4M",
    )
    monkeypatch.setattr("atlas.cli.load_config", lambda: settings)
    monkeypatch.setattr("atlas.planner.which", lambda _name: "/opt/bin/aria2c")

    file_result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "aria2",
            "--dry-run",
            "--json",
        ],
    )
    video_result = runner.invoke(
        app,
        ["video", "https://example.com/watch?v=1", "--dry-run", "--json"],
    )

    assert file_result.exit_code == 0
    file_payload = json.loads(file_result.output)
    assert file_payload["summary"]["connections"] == 7
    assert file_payload["summary"]["splits"] == 6
    assert file_payload["summary"]["chunk_size"] == "4M"
    assert video_result.exit_code == 0
    assert '"-x7"' in video_result.output
    assert '"-s6"' in video_result.output
    assert '"-k4M"' in video_result.output


def test_file_dry_run_json_accepts_wget2_backend() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "wget2",
            "--connections",
            "6",
            "--chunk-size",
            "4M",
            "--rate-limit",
            "2M",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"backend": "wget2"' in result.output
    assert '"engine": "wget2"' in result.output
    assert "--output-document" in result.output
    assert "--max-threads=6" in result.output
    assert "--chunk-size=4M" in result.output
    assert "--limit-rate=2M" in result.output


def test_file_dry_run_json_rejects_sensitive_headers_on_redirecting_backend() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "wget2",
            "--load-cookies",
            "/tmp/atlas-secret-cookies.txt",
            "--http-password",
            "super-secret",
            "--header",
            "Authorization: Bearer secret-token",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "super-secret" not in result.output
    assert "secret-token" not in result.output
    assert "/tmp/atlas-secret-cookies.txt" not in result.output
    assert payload["status"] == "error"
    assert "Sensitive custom headers require the native backend" in payload["error"]["message"]


def test_file_dry_run_json_shows_aria2_policy_options() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "aria2",
            "--lowest-speed-limit",
            "32K",
            "--max-tries",
            "5",
            "--retry-wait",
            "2.5",
            "--connect-timeout",
            "9",
            "--file-allocation",
            "trunc",
            "--check-integrity",
            "--remote-time",
            "--conditional-get",
            "--no-http-accept-gzip",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"backend": "aria2"' in result.output
    assert '"lowest_speed_limit": "32K"' in result.output
    assert '"max_tries": 5' in result.output
    assert '"retry_wait": 2.5' in result.output
    assert '"connect_timeout": 9.0' in result.output
    assert '"file_allocation": "trunc"' in result.output
    assert '"check_integrity": true' in result.output
    assert '"remote_time": true' in result.output
    assert '"conditional_get": true' in result.output
    assert '"http_accept_gzip": false' in result.output


def test_file_dry_run_json_shows_aria2_session_and_metalink_options() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/release.meta4",
            "--backend",
            "aria2",
            "--input-file",
            "/tmp/aria2.session",
            "--save-session",
            "/tmp/aria2.next",
            "--save-session-interval",
            "30",
            "--metalink-preferred-protocol",
            "https",
            "--metalink-language",
            "en-US",
            "--metalink-os",
            "macos",
            "--metalink-location",
            "us",
            "--metalink-base-uri",
            "https://mirrors.example/releases/",
            "--no-metalink-enable-unique-protocol",
            "--server-stat-if",
            "/tmp/servers.in",
            "--server-stat-of",
            "/tmp/servers.out",
            "--server-stat-timeout",
            "3600",
            "--uri-selector",
            "adaptive",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"backend": "aria2"' in result.output
    assert '"input_file": "/tmp/aria2.session"' in result.output
    assert '"save_session": "/tmp/aria2.next"' in result.output
    assert '"save_session_interval": 30' in result.output
    assert '"metalink_preferred_protocol": "https"' in result.output
    assert '"metalink_language": "en-US"' in result.output
    assert '"metalink_os": "macos"' in result.output
    assert '"metalink_location": "us"' in result.output
    assert '"metalink_base_uri": "https://mirrors.example/releases/"' in result.output
    assert '"metalink_enable_unique_protocol": false' in result.output
    assert '"server_stat_if": "/tmp/servers.in"' in result.output
    assert '"server_stat_of": "/tmp/servers.out"' in result.output
    assert '"server_stat_timeout": 3600' in result.output
    assert '"uri_selector": "adaptive"' in result.output


def test_file_dry_run_human_uses_smart_plan_preview() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/release.zip",
            "--backend",
            "aria2",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Dry Run Plan" in result.output
    assert "Equivalent Backend Command" in result.output
    assert "rerun with --json" in result.output
    assert '"route":' not in result.output
    assert '"summary":' not in result.output


def test_file_metalink_native_error_includes_recovery_hint() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/release.meta4",
            "--backend",
            "native",
        ],
    )

    assert result.exit_code == 1
    assert "Native file downloads cannot expand Metalink manifests" in result.output
    assert "Hint:" in result.output
    assert "--backend aria2" in result.output


def test_get_dry_run_routes_metalink_as_manifest() -> None:
    result = runner.invoke(
        app,
        [
            "get",
            "https://example.com/release.meta4",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"kind": "manifest"' in result.output
    assert '"force_metalink": true' in result.output


def test_file_adaptive_dry_run_skips_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_probe(_url: str) -> DirectFileProbe:
        raise AssertionError("dry-run should not probe the network")

    monkeypatch.setattr("atlas.optimizer.probe_direct_file", fail_probe)

    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--adaptive",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"adaptive"' in result.output
    assert "dry run: probe skipped" in result.output
    assert "unknown sizes" in result.output


def test_file_adaptive_explain_selects_large_file_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=700 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".iso",
            host="example.com",
            final_host="example.com",
        ),
    )
    monkeypatch.setattr(
        "atlas.cli.DirectFileAdapter.run",
        lambda *_args, **_kwargs: pytest.fail("explain should not download"),
    )

    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/big.iso",
            "--adaptive",
            "--explain",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"backend": "aria2"' in result.output
    assert '"per_file_segments": 8' in result.output
    assert "large files" in result.output


def test_site_adaptive_explain_includes_scan_hints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            kind=HubKind.site,
            content_type="text/html",
            discovered_links=["https://example.com/a", "https://cdn.example.com/app.js"],
            sitemap_urls=["https://example.com/sitemap.xml"],
            robots_url="https://example.com/robots.txt",
            external_host=True,
            size_class=FileSizeClass.small,
        ),
    )

    result = runner.invoke(
        app,
        [
            "site",
            "https://example.com/docs/",
            "--adaptive",
            "--explain",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "crawler queue" in result.output
    assert "https://example.com/sitemap.xml" in result.output
    assert "https://example.com/robots.txt" in result.output


def test_batch_adaptive_explain_uses_many_small_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n".join(f"https://example.com/{index}.txt" for index in range(5)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "atlas.cli.scan_direct_file",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            content_length=64 * 1024,
            size_class=FileSizeClass.tiny,
        ),
    )

    result = runner.invoke(
        app,
        [
            "batch",
            str(batch_file),
            "--adaptive",
            "--explain",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"queue_concurrency": 5' in result.output
    assert '"per_file_segments": 1' in result.output
    assert "many small files" in result.output


def test_batch_adaptive_explain_includes_media_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://www.youtube.com/watch?v=abc\nhttps://example.com/file.txt\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "atlas.cli.scan_direct_file",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            content_length=64 * 1024,
            size_class=FileSizeClass.tiny,
        ),
    )

    result = runner.invoke(
        app,
        [
            "batch",
            str(batch_file),
            "--adaptive",
            "--explain",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    adaptive = data["adaptive"]
    assert adaptive["bucket_counts"]["media"] == 1
    assert adaptive["bucket_counts"]["tiny"] == 1
    item_by_url = {item["url"]: item for item in adaptive["work_items"]}
    media_item = item_by_url["https://www.youtube.com/watch?v=abc"]
    assert media_item["bucket"] == "media"
    assert media_item["selected_backend"] == "yt-dlp"
    assert "media" in media_item["scheduler_decision"]


def test_batch_file_items_can_use_shared_aria2_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.aria2_rpc import Aria2RpcDownloadResult, Aria2RpcQueuedDownloadResult
    from atlas.cli import _try_run_aria2_batch_queue

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://example.com/one.iso\nhttps://example.com/two.iso\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=700 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".iso",
            host="example.com",
            final_host="example.com",
        ),
    )
    created: list[int | None] = []
    queued_urls: list[str] = []

    class FakeAria2Session:
        def __init__(
            self,
            *,
            max_concurrent_downloads: int | None = None,
            **_kwargs: object,
        ) -> None:
            created.append(max_concurrent_downloads)

        def download_many(self, queued):
            queued_urls.extend(item.options.url for item in queued)
            return [
                Aria2RpcQueuedDownloadResult(
                    item=item,
                    result=Aria2RpcDownloadResult(
                        gid=f"gid-{index}",
                        output=item.output,
                        status={},
                    ),
                )
                for index, item in enumerate(queued, start=1)
            ]

    monkeypatch.setattr("atlas.cli.Aria2RpcSession", FakeAria2Session)
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")

    summary = _try_run_aria2_batch_queue(
        settings,
        file=batch_file,
        kind=BatchKind.auto,
        output_dir=settings.output_dir,
        backend="aria2",
        allow_sites=False,
        resolved_concurrency=2,
        video_codec=VideoCodecChoice.auto,
        audio_codec=None,
        audio_quality=None,
        adaptive=False,
        max_concurrency=None,
        per_host_concurrency=None,
        politeness=AdaptivePoliteness.normal,
        verbose=False,
        reporter=None,
    )

    assert summary is not None
    assert created == [2]
    assert queued_urls == ["https://example.com/one.iso", "https://example.com/two.iso"]
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert summary.results[0].plan["queue"]["session"] == "shared"


def test_batch_file_filename_overrides_scope_duplicate_basenames() -> None:
    from atlas.cli import _batch_file_filename_overrides

    overrides = _batch_file_filename_overrides(
        [
            BatchEntry(
                line_no=1,
                url="http://pdf.textfiles.com/academics/bestpractices.pdf",
            ),
            BatchEntry(
                line_no=2,
                url="http://pdf.textfiles.com/pamphlets/bestpractices.pdf",
            ),
            BatchEntry(line_no=3, url="http://pdf.textfiles.com/books/book.pdf"),
        ]
    )

    assert overrides == {
        1: "academics__bestpractices.pdf",
        2: "pamphlets__bestpractices.pdf",
    }


def test_batch_shared_aria2_queue_yields_to_runtime_on_tls_chain_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.aria2_rpc import Aria2RpcQueuedDownloadResult
    from atlas.cli import _try_run_aria2_batch_queue

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://example.com/one.iso\nhttps://example.com/two.iso\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=700 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".iso",
            host="example.com",
            final_host="example.com",
        ),
    )

    class FakeAria2Session:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def download_many(self, queued):
            for item in queued:
                if item.progress_callback:
                    item.progress_callback(
                        ProgressEvent(
                            engine=EngineKind.aria2,
                            status="error",
                            phase=ProgressPhase.error,
                            kind=HubKind.file,
                            url=item.options.url,
                            message=(
                                "aria2 error 1: SSL/TLS handshake failure: "
                                "unable to get local issuer certificate"
                            ),
                        )
                    )
            return [
                Aria2RpcQueuedDownloadResult(
                    item=item,
                    error=(
                        "aria2 error 1: SSL/TLS handshake failure: "
                        "unable to get local issuer certificate"
                    ),
                )
                for item in queued
            ]

    class FakeReporter:
        def __init__(self) -> None:
            self.events: list[ProgressEvent] = []

        def hook(self, event: ProgressEvent) -> None:
            self.events.append(event)

    monkeypatch.setattr("atlas.cli.Aria2RpcSession", FakeAria2Session)
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    reporter = FakeReporter()

    summary = _try_run_aria2_batch_queue(
        settings,
        file=batch_file,
        kind=BatchKind.auto,
        output_dir=settings.output_dir,
        backend="aria2",
        allow_sites=False,
        resolved_concurrency=2,
        video_codec=VideoCodecChoice.auto,
        audio_codec=None,
        audio_quality=None,
        adaptive=False,
        max_concurrency=None,
        per_host_concurrency=None,
        politeness=AdaptivePoliteness.normal,
        verbose=False,
        reporter=reporter,
    )

    assert summary is None
    assert [(event.status, event.engine, event.phase) for event in reporter.events] == [
        ("retrying", EngineKind.curl, ProgressPhase.download),
        ("retrying", EngineKind.curl, ProgressPhase.download),
    ]
    assert all(event.line_no in {1, 2} for event in reporter.events)
    assert all("verified curl fallback" in (event.message or "") for event in reporter.events)


def test_batch_shared_aria2_queue_skips_known_tls_curl_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.cli import _try_run_aria2_batch_queue

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://example.com/one.epub\nhttps://example.com/two.epub\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            file_extension=".epub",
            host="example.com",
            final_host="example.com",
            error="TLS certificate verification failed",
            probed=False,
        ),
    )

    class UnexpectedAria2Session:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("shared aria2 queue should not start for known TLS fallback")

    class FakeReporter:
        def __init__(self) -> None:
            self.events: list[ProgressEvent] = []

        def hook(self, event: ProgressEvent) -> None:
            self.events.append(event)

    monkeypatch.setattr("atlas.cli.Aria2RpcSession", UnexpectedAria2Session)
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")
    reporter = FakeReporter()

    summary = _try_run_aria2_batch_queue(
        settings,
        file=batch_file,
        kind=BatchKind.auto,
        output_dir=settings.output_dir,
        backend="aria2",
        allow_sites=False,
        resolved_concurrency=2,
        video_codec=VideoCodecChoice.auto,
        audio_codec=None,
        audio_quality=None,
        adaptive=False,
        max_concurrency=None,
        per_host_concurrency=None,
        politeness=AdaptivePoliteness.normal,
        verbose=False,
        reporter=reporter,
    )

    assert summary is None
    assert reporter.events == []


def test_batch_result_event_uses_actual_fallback_backend(tmp_path: Path) -> None:
    url = "https://example.com/book.epub"
    route = EngineRoute(
        kind=HubKind.file,
        engine=EngineKind.aria2,
        reason="test",
        url=url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(route=route, output=tmp_path / "book.epub"),
        options=FileDownloadOptions(url=url, output_dir=tmp_path),
    )
    result = DownloadResult(
        status=DownloadStatus.success,
        url=url,
        message="Saved with fallback",
        ydl_opts={
            "route": route.model_dump(mode="json"),
            "result": {
                "backend": "curl",
                "fallback_from": "aria2",
                "output": str(tmp_path / "book.epub"),
            },
        },
    )

    class FakeReporter:
        def __init__(self) -> None:
            self.events: list[ProgressEvent] = []

        def hook(self, event: ProgressEvent) -> None:
            self.events.append(event)

    reporter = FakeReporter()

    cli._emit_batch_result_event(
        reporter,
        BatchEntry(line_no=1, url=url),
        result,
        plan=plan,
    )

    assert [(event.status, event.engine, event.phase) for event in reporter.events] == [
        ("done", EngineKind.curl, ProgressPhase.done)
    ]


def test_batch_shared_aria2_queue_applies_duplicate_filename_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.aria2_rpc import Aria2RpcDownloadResult, Aria2RpcQueuedDownloadResult
    from atlas.cli import _batch_file_filename_overrides, _try_run_aria2_batch_queue

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://example.com/one/file.iso\nhttps://example.com/two/file.iso\n",
        encoding="utf-8",
    )
    entries, _skipped = (
        [
            BatchEntry(line_no=1, url="https://example.com/one/file.iso"),
            BatchEntry(line_no=2, url="https://example.com/two/file.iso"),
        ],
        0,
    )
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=700 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".iso",
            host="example.com",
            final_host="example.com",
        ),
    )
    queued_outputs: list[str] = []

    class FakeAria2Session:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def download_many(self, queued):
            queued_outputs.extend(item.output.name for item in queued)
            return [
                Aria2RpcQueuedDownloadResult(
                    item=item,
                    result=Aria2RpcDownloadResult(
                        gid=f"gid-{index}",
                        output=item.output,
                        status={},
                    ),
                )
                for index, item in enumerate(queued, start=1)
            ]

    monkeypatch.setattr("atlas.cli.Aria2RpcSession", FakeAria2Session)
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")

    summary = _try_run_aria2_batch_queue(
        settings,
        file=batch_file,
        kind=BatchKind.auto,
        output_dir=settings.output_dir,
        backend="aria2",
        allow_sites=False,
        resolved_concurrency=2,
        video_codec=VideoCodecChoice.auto,
        audio_codec=None,
        audio_quality=None,
        adaptive=False,
        max_concurrency=None,
        per_host_concurrency=None,
        politeness=AdaptivePoliteness.normal,
        verbose=False,
        reporter=None,
        filename_overrides=_batch_file_filename_overrides(entries),
    )

    assert summary is not None
    assert summary.succeeded == 2
    assert queued_outputs == ["one__file.iso", "two__file.iso"]


def test_batch_shared_queue_reserves_casefolded_probe_filenames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.aria2_rpc import Aria2RpcDownloadResult, Aria2RpcQueuedDownloadResult
    from atlas.cli import _try_run_aria2_batch_queue

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://example.com/releases/one\nhttps://example.com/releases/two\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")

    def probe(url: str) -> DirectFileProbe:
        filename = "Report.pdf" if url.endswith("one") else "report.pdf"
        return DirectFileProbe(
            url=url,
            final_url=url,
            content_length=700 * 1024 * 1024,
            filename=filename,
            supports_ranges=True,
            file_extension=".pdf",
            host="example.com",
            final_host="example.com",
        )

    monkeypatch.setattr("atlas.optimizer.probe_direct_file", probe)
    queued_outputs: list[str] = []

    class FakeAria2Session:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def download_many(self, queued):
            queued_outputs.extend(item.output.name for item in queued)
            return [
                Aria2RpcQueuedDownloadResult(
                    item=item,
                    result=Aria2RpcDownloadResult(
                        gid=f"gid-{index}",
                        output=item.output,
                        status={},
                    ),
                )
                for index, item in enumerate(queued, start=1)
            ]

    monkeypatch.setattr("atlas.cli.Aria2RpcSession", FakeAria2Session)
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")

    summary = _try_run_aria2_batch_queue(
        settings,
        file=batch_file,
        kind=BatchKind.file,
        output_dir=settings.output_dir,
        backend="aria2",
        allow_sites=False,
        resolved_concurrency=2,
        video_codec=VideoCodecChoice.auto,
        audio_codec=None,
        audio_quality=None,
        adaptive=False,
        max_concurrency=None,
        per_host_concurrency=None,
        politeness=AdaptivePoliteness.normal,
        verbose=False,
        reporter=None,
    )

    assert summary is not None
    assert summary.succeeded == 2
    assert len({name.casefold() for name in queued_outputs}) == 2
    assert queued_outputs[0] == "Report.pdf"
    assert queued_outputs[1].endswith("__report.pdf")


def test_batch_shared_queue_retries_only_tls_failed_items_with_curl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.aria2_rpc import (
        Aria2RpcDownloadResult,
        Aria2RpcQueuedDownloadResult,
    )
    from atlas.cli import _try_run_aria2_batch_queue

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://example.com/one.iso\nhttps://example.com/two.iso\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=700 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".iso",
            host="example.com",
            final_host="example.com",
        ),
    )

    class FakeAria2Session:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def download_many(self, queued):
            return [
                Aria2RpcQueuedDownloadResult(
                    item=queued[0],
                    result=Aria2RpcDownloadResult(
                        gid="gid-1",
                        output=queued[0].output,
                        status={},
                    ),
                ),
                Aria2RpcQueuedDownloadResult(
                    item=queued[1],
                    error=("SSL/TLS handshake failure: unable to get local issuer certificate"),
                ),
            ]

    retried: list[str] = []

    def retry_curl(self, options, *, output, progress_callback=None, message):
        _ = self, progress_callback, message
        retried.append(options.url)
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message=f"Saved to {output} (curl TLS fallback)",
            ydl_opts={"backend": "curl", "output": str(output)},
        )

    monkeypatch.setattr("atlas.cli.Aria2RpcSession", FakeAria2Session)
    monkeypatch.setattr(
        "atlas.cli.FileDownloadEngine.download_with_verified_curl",
        retry_curl,
    )
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")

    summary = _try_run_aria2_batch_queue(
        settings,
        file=batch_file,
        kind=BatchKind.file,
        output_dir=settings.output_dir,
        backend="aria2",
        allow_sites=False,
        resolved_concurrency=2,
        video_codec=VideoCodecChoice.auto,
        audio_codec=None,
        audio_quality=None,
        adaptive=False,
        max_concurrency=None,
        per_host_concurrency=None,
        politeness=AdaptivePoliteness.normal,
        verbose=False,
        reporter=None,
    )

    assert summary is not None
    assert summary.succeeded == 2
    assert summary.failed == 0
    assert retried == ["https://example.com/two.iso"]
    assert summary.results[1].plan["queue"]["engine"] == "curl"


def test_batch_shared_aria2_queue_receives_adaptive_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.aria2_rpc import Aria2RpcDownloadResult, Aria2RpcQueuedDownloadResult
    from atlas.cli import _try_run_aria2_batch_queue
    from atlas.models import AdaptiveDownloadPlan

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://example.com/one.iso\nhttps://example.com/two.iso\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("atlas.optimizer.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=700 * 1024 * 1024,
            supports_ranges=True,
            file_extension=".iso",
            host="example.com",
            final_host="example.com",
        ),
    )
    received: list[tuple[int, int] | None] = []

    class FakeAria2Session:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def download_many(self, queued, *, adaptive_scheduler=None):
            if adaptive_scheduler is None:
                received.append(None)
            else:
                received.append(
                    (
                        adaptive_scheduler.global_max_concurrency,
                        adaptive_scheduler.per_host_concurrency,
                    )
                )
            return [
                Aria2RpcQueuedDownloadResult(
                    item=item,
                    result=Aria2RpcDownloadResult(
                        gid=f"gid-{index}",
                        output=item.output,
                        status={},
                    ),
                )
                for index, item in enumerate(queued, start=1)
            ]

    monkeypatch.setattr("atlas.cli.Aria2RpcSession", FakeAria2Session)
    settings = AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")

    summary = _try_run_aria2_batch_queue(
        settings,
        file=batch_file,
        kind=BatchKind.auto,
        output_dir=settings.output_dir,
        backend="aria2",
        allow_sites=False,
        resolved_concurrency=2,
        video_codec=VideoCodecChoice.auto,
        audio_codec=None,
        audio_quality=None,
        adaptive=True,
        adaptive_plan=AdaptiveDownloadPlan(
            enabled=True,
            queue_concurrency=2,
            per_host_concurrency=1,
            global_max_concurrency=8,
            politeness=AdaptivePoliteness.normal,
        ),
        max_concurrency=8,
        per_host_concurrency=1,
        politeness=AdaptivePoliteness.normal,
        verbose=False,
        reporter=None,
    )

    assert summary is not None
    assert summary.succeeded == 2
    assert received == [(2, 1)]


def test_file_summary_shows_probe_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda _url: DirectFileProbe(
            url="https://example.com/download",
            final_url="https://cdn.example.com/Installer.dmg",
            redirected=True,
            content_type="application/x-apple-diskimage",
            content_length=734_003_200,
            supports_ranges=True,
            filename="Installer.dmg",
            file_extension=".dmg",
        ),
    )

    def fake_run(self, options, *, progress_callback=None):
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message="Saved to Installer.dmg",
        )

    monkeypatch.setattr("atlas.cli.DirectFileAdapter.run", fake_run)

    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/download",
            "--backend",
            "native",
            "--checksum",
            "sha256:" + "a" * 64,
            "--progress",
            "none",
        ],
    )

    assert result.exit_code == 0
    assert "File Download" in result.output
    assert "700.0 MB" in result.output
    assert "application/x-apple-diskimage" in result.output
    assert "supported" in result.output
    assert "https://cdn.example.com/Installer.dmg" in result.output
    assert "sha256" in result.output


def test_file_runtime_json_is_one_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "atlas.cli.DirectFileAdapter.run",
        lambda self, options, **_kwargs: DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message="saved",
        ),
    )

    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "native",
            "--output-dir",
            str(tmp_path),
            "--json",
            "--progress",
            "compact",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "success"
    assert payload["kind"] == "file"
    assert payload["url"] == "https://example.com/archive.zip"
    assert payload["files"] == [str(tmp_path / "archive.zip")]


def test_site_dry_run_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(
        app,
        [
            "site",
            "https://example.com/docs/",
            "--backend",
            "wget2",
            "--depth",
            "1",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"backend": "wget2"' in result.output
    assert "--recursive" in result.output
    assert "--level=1" in result.output


def test_site_runtime_json_is_one_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.cli.SiteMirrorAdapter.run",
        lambda self, options, **_kwargs: DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message="saved",
            ydl_opts={"backend": "wget2", "output": str(options.output_dir)},
        ),
    )

    result = runner.invoke(
        app,
        [
            "site",
            "https://example.com/docs/",
            "--backend",
            "wget2",
            "--output-dir",
            str(tmp_path),
            "--json",
            "--progress",
            "full",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "success"
    assert payload["kind"] == "site"
    assert payload["url"] == "https://example.com/docs/"
    assert payload["files"] == [str(tmp_path)]


def test_site_accepts_page_requisites_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(
        app,
        [
            "site",
            "https://example.com/docs/",
            "--backend",
            "wget2",
            "--page-requisites",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"assets": true' in result.output
    assert "--page-requisites" in result.output


def test_site_dry_run_json_redacts_sensitive_backend_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(
        app,
        [
            "site",
            "https://example.com/docs/",
            "--backend",
            "wget2",
            "--load-cookies",
            "/tmp/site-cookies.txt",
            "--save-cookies",
            "/tmp/site-saved-cookies.txt",
            "--http-password",
            "site-secret",
            "--proxy-password",
            "proxy-secret",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "site-secret" not in result.output
    assert "proxy-secret" not in result.output
    assert "/tmp/site-cookies.txt" not in result.output
    assert "/tmp/site-saved-cookies.txt" not in result.output
    assert "--http-password=<redacted>" in result.output
    assert "--proxy-password=<redacted>" in result.output
    assert "--load-cookies=<redacted>" in result.output
    assert "--save-cookies=<redacted>" in result.output


def test_dir_dry_run_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--depth",
            "2",
            "--accept",
            "zip,7z,pdf,mp4",
            "--reject",
            "html,tmp",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    args = payload["args"]
    assert '"kind": "dir"' in result.output
    assert '"mirror_kind": "dir"' in result.output
    assert payload["summary"]["timestamping"] is True
    assert payload["summary"]["if_modified_since"] is False
    assert payload["summary"]["resume"] is True
    assert args[:10] == [
        "/opt/bin/wget2",
        "--recursive",
        "--no-parent",
        "--mirror",
        "--continue",
        "--timestamping",
        "--no-if-modified-since",
        f"--directory-prefix={payload['output']}",
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "--level=2",
    ]
    assert args.index("--level=2") > args.index("--mirror")
    assert args[-1] == "https://example.com/files/"
    assert "--recursive" in result.output
    assert "--mirror" in result.output
    assert "--level=2" in result.output
    assert "--no-parent" in result.output
    assert "--continue" in result.output
    assert "--timestamping" in result.output
    assert "--no-if-modified-since" in result.output
    assert "--directory-prefix=" in result.output
    assert "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" in result.output
    assert "--accept=zip,7z,pdf,mp4" in result.output
    assert "--reject=html,tmp" in result.output
    assert "--page-requisites" not in result.output
    assert "--convert-links" not in result.output


def test_dir_dry_run_json_allows_wget2_policy_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--no-timestamping",
            "--if-modified-since",
            "--user-agent",
            "AtlasTest/1.0",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "--timestamping" not in result.output
    assert "--if-modified-since" in result.output
    assert "--no-if-modified-since" not in result.output
    assert "--user-agent=AtlasTest/1.0" in result.output


def test_dir_adaptive_explain_includes_crawler_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            kind=HubKind.dir,
            content_type="text/html",
            discovered_links=["https://example.com/files/a.pdf"],
            sitemap_urls=[],
            robots_url="https://example.com/robots.txt",
            size_class=FileSizeClass.small,
        ),
    )

    result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--adaptive",
            "--explain",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["route"]["kind"] == "dir"
    assert data["summary"]["adaptive"]["work_items"][0]["kind"] == "dir"
    assert "crawler queue with per-host politeness" in result.output
    assert "https://example.com/robots.txt" in result.output


def test_dir_adaptive_explain_human_uses_smart_plan_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            kind=HubKind.dir,
            content_type="text/html",
            discovered_links=["https://example.com/files/a.pdf"],
            robots_url="https://example.com/robots.txt",
            size_class=FileSizeClass.small,
        ),
    )

    result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--adaptive",
            "--explain",
        ],
    )

    assert result.exit_code == 0
    assert "Explain Plan" in result.output
    assert "Scheduler" in result.output
    assert "Equivalent Backend Command" in result.output
    assert "crawler queue with per-host politeness" in result.output
    assert '"route":' not in result.output
    assert '"summary":' not in result.output


def test_dir_adaptive_explain_human_surfaces_failed_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            host="example.com",
            kind=HubKind.dir,
            scan_type="failed scan",
            scan_status=ScanStatus.failed,
            scan_recommended_strategy="retry, run doctor, or continue as backend mirror",
            scan_errors=[
                {
                    "code": "http_status",
                    "message": "HTTP 403: Forbidden",
                    "url": url,
                    "recoverable": True,
                }
            ],
            error="HTTP 403: Forbidden",
        ),
    )

    result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--adaptive",
            "--explain",
        ],
    )

    assert result.exit_code == 0
    assert "Discovery" in result.output
    assert "Scan" in result.output
    assert "failed · HTTP 403: Forbidden" in result.output
    assert "backend mirror plan continues without verified discovery" in result.output
    assert "Equivalent Backend Command" in result.output


def test_dir_adaptive_explain_preserves_partial_root_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            final_url=url,
            host="example.com",
            kind=HubKind.dir,
            scan_type="directory-style HTML index",
            scan_status=ScanStatus.partial,
            scan_counts={"links": 2_000, "same_host": 1, "complete": 0},
            scan_warnings=["link extraction stopped at the 2,000-link safety limit"],
            discovered_work_items=[
                WorkItem(
                    url=f"{url}visible.pdf",
                    host="example.com",
                    kind=HubKind.file,
                )
            ],
        ),
    )

    json_result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--adaptive",
            "--explain",
            "--json",
        ],
    )
    human_result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--adaptive",
            "--explain",
        ],
    )

    assert json_result.exit_code == 0
    adaptive = json.loads(json_result.output)["summary"]["adaptive"]
    assert adaptive["scan_status"] == "partial"
    assert adaptive["scan_counts"]["complete"] == 0
    assert adaptive["scan_warnings"] == ["link extraction stopped at the 2,000-link safety limit"]
    assert human_result.exit_code == 0
    assert "Discovery" in human_result.output
    assert "partial" in human_result.output
    assert "visible discovery is incomplete" in human_result.output
    assert "2,000-link safety limit" in human_result.output


def test_dir_adaptive_explain_human_surfaces_empty_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        "atlas.optimizer.scan_site",
        lambda url, *, dry_run: WorkItem(
            url=url,
            final_url=url,
            host="example.com",
            kind=HubKind.dir,
            scan_type="directory-style HTML index",
            scan_status=ScanStatus.empty,
            scan_counts={"links": 0, "same_host": 0, "complete": 1},
        ),
    )

    result = runner.invoke(
        app,
        [
            "dir",
            "https://example.com/files/",
            "--backend",
            "wget2",
            "--adaptive",
            "--explain",
        ],
    )

    assert result.exit_code == 0
    assert "Discovery" in result.output
    assert "empty" in result.output
    assert "no downloadable links were discovered" in result.output


def test_site_dry_run_json_accepts_wget2_policy_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(
        app,
        [
            "site",
            "https://example.com/docs/",
            "--backend",
            "wget2",
            "--no-robots",
            "--no-follow-sitemaps",
            "--domains",
            "example.com",
            "--same-domain-www",
            "--accept-regex",
            ".*\\.html$",
            "--filter-mime-type",
            "text/html",
            "--filter-urls",
            "--follow-tags",
            "img/data-src",
            "--ignore-tags",
            "a/href",
            "--no-directories",
            "--protocol-directories",
            "--cut-dirs",
            "1",
            "--default-page",
            "home.html",
            "--adjust-extension",
            "--convert-file-only",
            "--cut-url-get-vars",
            "--cut-file-get-vars",
            "--keep-extension",
            "--unlink",
            "--overwrite",
            "--download-attr",
            "strippath",
            "--force-html",
            "--force-css",
            "--force-metalink",
            "--warc-file",
            "archive.warc.gz",
            "--warc-compression",
            "--warc-cdx",
            "--warc-max-size",
            "1G",
            "--user-agent",
            "AtlasTest/1.0",
            "--header",
            "X-Test: yes",
            "--referer",
            "https://referrer.example/",
            "--no-cache",
            "--compression",
            "br",
            "--cookies-from-browser",
            "safari",
            "--https-only",
            "--https-enforce",
            "hard",
            "--no-hsts",
            "--hsts-file",
            "hsts.db",
            "--no-check-certificate",
            "--no-check-hostname",
            "--certificate-type",
            "PEM",
            "--private-key-type",
            "DER",
            "--crl-file",
            "revocations.pem",
            "--ocsp",
            "--no-ocsp-date",
            "--ocsp-file",
            "ocsp.db",
            "--no-ocsp-nonce",
            "--ocsp-server",
            "http://ocsp.example/",
            "--ocsp-stapling",
            "--tls-false-start",
            "--tls-resume",
            "--tls-session-file",
            "tls-sessions.db",
            "--http2",
            "--http2-only",
            "--content-on-error",
            "--save-content-on",
            "500",
            "--save-headers",
            "--server-response",
            "--ignore-length",
            "--verify-sig",
            "no-fail",
            "--signature-extensions",
            "asc,sig",
            "--gnupg-homedir",
            "gnupg",
            "--verify-save-failed",
            "--max-total-size",
            "10M",
            "--max-runtime",
            "60",
            "--quota",
            "10M",
            "--limit-rate",
            "1M",
            "--inet4-only",
            "--bind-address",
            "127.0.0.1",
            "--bind-interface",
            "lo0",
            "--prefer-family",
            "IPv6",
            "--no-dns-cache",
            "--dns-cache-preload",
            "dns-cache.txt",
            "--no-tcp-fastopen",
            "--max-threads",
            "3",
            "--tries",
            "2",
            "--waitretry",
            "0.5",
            "--retry-on-http-error",
            "429,503",
            "--max-redirect",
            "4",
            "--timeout",
            "7",
            "--check",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "--no-robots" in result.output
    assert "--no-follow-sitemaps" in result.output
    assert "--span-hosts" in result.output
    assert "--domains=example.com,www.example.com" in result.output
    assert "--accept-regex=.*\\\\.html$" in result.output
    assert "--filter-mime-type=text/html" in result.output
    assert "--filter-urls" in result.output
    assert "--follow-tags=img/data-src" in result.output
    assert "--ignore-tags=a/href" in result.output
    assert "--no-directories" in result.output
    assert "--protocol-directories" in result.output
    assert "--cut-dirs=1" in result.output
    assert "--default-page=home.html" in result.output
    assert "--adjust-extension" in result.output
    assert "--convert-file-only" in result.output
    assert "--cut-url-get-vars" in result.output
    assert "--cut-file-get-vars" in result.output
    assert "--keep-extension" in result.output
    assert "--unlink" in result.output
    assert "--continue" in result.output
    assert "--clobber" in result.output
    assert "--download-attr=strippath" in result.output
    assert "--force-html" in result.output
    assert "--force-css" in result.output
    assert "--force-metalink" in result.output
    assert "--warc-file=archive" in result.output
    assert "--warc-compression" in result.output
    assert "--warc-cdx" in result.output
    assert "--warc-max-size=1G" in result.output
    assert "--user-agent=AtlasTest/1.0" in result.output
    assert "--header=X-Test: yes" in result.output
    assert "--referer=https://referrer.example/" in result.output
    assert "--no-cache" in result.output
    assert "--compression=br" in result.output
    assert '"browser_cookies": true' in result.output
    assert "--https-only" in result.output
    assert "--https-enforce=hard" in result.output
    assert "--no-hsts" in result.output
    assert "--hsts-file=hsts.db" in result.output
    assert "--no-check-certificate" in result.output
    assert "--no-check-hostname" in result.output
    assert "--certificate-type=PEM" in result.output
    assert "--private-key-type=DER" in result.output
    assert "--crl-file=revocations.pem" in result.output
    assert "--ocsp" in result.output
    assert "--no-ocsp-date" in result.output
    assert "--ocsp-file=ocsp.db" in result.output
    assert "--no-ocsp-nonce" in result.output
    assert "--ocsp-server=http://ocsp.example/" in result.output
    assert "--ocsp-stapling" in result.output
    assert "--tls-false-start" in result.output
    assert "--tls-resume" in result.output
    assert "--tls-session-file=tls-sessions.db" in result.output
    assert "--http2" in result.output
    assert "--http2-only" in result.output
    assert "--content-on-error" in result.output
    assert "--save-content-on=500" in result.output
    assert "--save-headers" in result.output
    assert "--server-response" in result.output
    assert "--ignore-length" in result.output
    assert "--verify-sig=no-fail" in result.output
    assert "--signature-extensions=asc,sig" in result.output
    assert "--gnupg-homedir=gnupg" in result.output
    assert "--verify-save-failed" in result.output
    assert '"max_files": null' in result.output
    assert '"max_total_size": "10M"' in result.output
    assert '"max_runtime": 60.0' in result.output
    assert "--quota=10M" in result.output
    assert "--limit-rate=1M" in result.output
    assert "--inet4-only" in result.output
    assert "--bind-address=127.0.0.1" in result.output
    assert "--bind-interface=lo0" in result.output
    assert "--prefer-family=IPv6" in result.output
    assert "--no-dns-cache" in result.output
    assert "--dns-cache-preload=dns-cache.txt" in result.output
    assert "--no-tcp-fastopen" in result.output
    assert "--max-threads=3" in result.output
    assert "--tries=2" in result.output
    assert "--waitretry=0.5" in result.output
    assert "--retry-on-http-error=429,503" in result.output
    assert "--max-redirect=4" in result.output
    assert "--timeout=7" in result.output
    assert "--spider" in result.output


def test_site_scope_toggles_are_mutually_exclusive() -> None:
    result = runner.invoke(
        app,
        [
            "site",
            "https://example.com/docs/",
            "--same-host-only",
            "--include-subdomains",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "Choose only one mirror scope" in result.output
    assert "Traceback" not in result.output


def test_site_from_file_mode_builds_input_parser_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    input_file = tmp_path / "urls.txt"
    input_file.write_text("https://example.com/sitemap.xml\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "site",
            "from-file",
            str(input_file),
            "--backend",
            "wget2",
            "--force-sitemap",
            "--base",
            "https://example.com/",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert f"--input-file={input_file}" in result.output
    assert "--force-sitemap" in result.output
    assert "--base=https://example.com/" in result.output
    assert '"input_file_only": true' in result.output


def test_site_from_file_without_base_accepts_local_parser_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    input_file = tmp_path / "urls.txt"
    input_file.write_text("https://example.com/file.zip\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["site", "from-file", str(input_file), "--backend", "wget2", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["summary"]["input_file_only"] is True
    assert f"--input-file={input_file}" in payload["args"]


def test_get_routes_media_to_video_dry_run() -> None:
    result = runner.invoke(
        app,
        [
            "get",
            "https://www.youtube.com/watch?v=abc",
            "--video-codec",
            "h264",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"video_codec": "h264"' in result.output
    assert "[vcodec^=avc1]" in result.output
    assert '"noplaylist": true' in result.output


def test_get_audio_codec_dry_run() -> None:
    result = runner.invoke(
        app,
        [
            "get",
            "https://www.youtube.com/watch?v=abc",
            "--kind",
            "audio",
            "--codec",
            "mp3",
            "--audio-quality",
            "3",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"codec": "mp3"' in result.output
    assert '"audio_quality": 3' in result.output


def test_get_explain_media_prints_plan_without_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli._engine",
        lambda _settings: pytest.fail("explain should not create a media engine"),
    )

    result = runner.invoke(
        app,
        ["get", "https://www.youtube.com/watch?v=abc", "--explain", "--json"],
    )

    assert result.exit_code == 0
    assert '"engine": "yt-dlp"' in result.output
    assert '"format": "bestvideo*+bestaudio/best"' in result.output


def test_get_routes_file_to_direct_download_dry_run() -> None:
    result = runner.invoke(
        app,
        [
            "get",
            "https://example.com/archive.zip",
            "--backend",
            "native",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"backend": "native"' in result.output
    assert "archive.zip" in result.output


def test_get_routes_file_to_wget2_dry_run() -> None:
    result = runner.invoke(
        app,
        [
            "get",
            "https://example.com/archive.zip",
            "--kind",
            "file",
            "--backend",
            "wget2",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"kind": "file"' in result.output
    assert '"backend": "wget2"' in result.output
    assert '"engine": "wget2"' in result.output
    assert "--output-document" in result.output
    assert "--max-threads=" in result.output


def test_get_routes_explicit_dir_to_directory_mirror_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")

    result = runner.invoke(
        app,
        [
            "get",
            "https://example.com/files/",
            "--kind",
            "dir",
            "--backend",
            "wget2",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"kind": "dir"' in result.output
    assert '"backend": "wget2"' in result.output
    assert "--recursive" in result.output
    assert "--mirror" in result.output
    assert "--no-parent" in result.output
    assert "--timestamping" in result.output
    assert "--no-if-modified-since" in result.output
    assert "--directory-prefix=" in result.output


def test_get_reports_invalid_backend_without_traceback() -> None:
    result = runner.invoke(
        app,
        [
            "get",
            "https://example.com/archive.zip",
            "--backend",
            "curl",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "--backend for file downloads must be auto, native, aria2, or wget2" in result.output
    assert "Traceback" not in result.output


def test_get_invalid_url_json_is_one_machine_error_document() -> None:
    result = runner.invoke(app, ["get", "not-a-url", "--dry-run", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "ValidationError"
    assert "Traceback" not in result.output


def test_file_reports_invalid_checksum_without_traceback() -> None:
    result = runner.invoke(
        app,
        [
            "file",
            "https://example.com/archive.zip",
            "--checksum",
            "broken",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "checksum must look like sha256:<hex-digest>" in result.output
    assert "Traceback" not in result.output


def test_video_download_prints_summary_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_hook = object()
    events: list[object] = []
    monkeypatch.setattr("atlas.planner.which", lambda _name: "/opt/bin/aria2c")

    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(
                id="abc123",
                title="Example Video",
                uploader="Example Channel",
                extractor="youtube",
                upload_date="20260607",
            )

        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            assert options.dry_run is False
            assert progress_hooks == [expected_hook]
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = ["/tmp/Example Video.mkv"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def hook(self, event):
            events.append(event)

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli.create_progress_hook", lambda _reporter: expected_hook)

    result = runner.invoke(app, ["video", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert "Download" in result.output
    assert "Example Video" in result.output
    assert "bestvideo*+bestaudio/best" in result.output
    assert "max" in result.output
    assert "mkv" in result.output
    assert "Download complete" in result.output
    assert any(
        getattr(event, "status", None) == "running"
        and getattr(event, "message", None) == "temporary .part files are normal until merge"
        for event in events
    )


@pytest.mark.parametrize("progress_mode", ["auto", "compact", "full", "json"])
def test_video_runtime_json_is_one_document(
    progress_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(id="abc123", title="Example Video", extractor="youtube")

        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = ["/tmp/Example Video.mkv"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def hook(self, _event):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)

    result = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--json",
            "--progress",
            progress_mode,
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "status": "success",
        "kind": "video",
        "url": "https://example.com/watch?v=1",
        "files": ["/tmp/Example Video.mkv"],
    }


def test_video_probe_receives_collection_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[InfoOptions] = []

    class FakeEngine:
        def get_info(self, options):
            seen.append(options)
            return MediaInfo(id="abc123", title="Example Video", extractor="youtube")

        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = ["/tmp/Example Video.mkv"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def hook(self, _event):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)

    result = runner.invoke(
        app,
        [
            "video",
            "https://www.youtube.com/@example/videos",
            "--playlist",
            "--playlist-items",
            "1",
            "--socket-timeout",
            "7",
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    assert len(seen) == 1
    probe = seen[0]
    assert probe.playlist is True
    assert probe.playlist_items == "1"
    assert probe.playlist_start is None
    assert probe.playlist_end is None
    assert probe.socket_timeout == 7


def test_collection_output_preview_uses_downloader_template(tmp_path: Path) -> None:
    outtmpl = str(
        tmp_path
        / "%(playlist_title|playlist)s"
        / "%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s"
    )
    options = VideoDownloadOptions(
        url="https://www.youtube.com/@AveryYapps/videos",
        output_dir=tmp_path,
        playlist=True,
        playlist_items="1",
        organize=OrganizeMode.playlist,
    )
    plan = DownloadPlan(
        url=options.url,
        output_dir=tmp_path,
        outtmpl=outtmpl,
        format="best",
        noplaylist=False,
        merge_output_format="mkv",
    )
    media = MediaInfo(
        id="UCU28LWFMn1GN0coMTBFTo2w",
        title="Avery Yapps - Videos",
        extractor="youtube:tab",
        is_playlist=True,
    )

    assert cli._output_preview(media=media, options=options, plan=plan, ext="mkv") == outtmpl


def test_video_download_summary_does_not_repeat_format_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
    )
    plan = DownloadPlan(
        url=options.url,
        output_dir=tmp_path,
        outtmpl=str(tmp_path / "%(title)s.%(ext)s"),
        format="best",
        noplaylist=True,
        merge_output_format="mkv",
    )
    media = MediaInfo(
        id="abc123",
        title="Example Video",
        extractor="youtube",
        formats=[FormatInfo(format_id="18", ext="mp4")],
    )

    monkeypatch.setattr(
        cli,
        "_print_smart_format_choices",
        lambda _formats: pytest.fail("download summary repeated the format catalog"),
    )

    cli._print_video_summary(media, options, plan)


def test_audio_runtime_json_is_one_document(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(id="abc123", title="Example Audio", extractor="youtube")

        def download_audio(self, options, progress_hooks=None, postprocessor_hooks=None):
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = ["/tmp/Example Audio.m4a"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def hook(self, _event):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)

    result = runner.invoke(
        app,
        [
            "audio",
            "https://example.com/watch?v=1",
            "--json",
            "--progress",
            "compact",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "status": "success",
        "kind": "audio",
        "url": "https://example.com/watch?v=1",
        "files": ["/tmp/Example Audio.m4a"],
    }


def test_media_work_context_only_lists_planned_steps(tmp_path: Path) -> None:
    single_stream = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        embed_metadata=False,
        write_thumbnail=False,
        embed_thumbnail=False,
    )
    video_plan = DownloadPlan(
        url=single_stream.url,
        output_dir=tmp_path,
        outtmpl="%(title)s.%(ext)s",
        format="best[height<=480]",
        noplaylist=True,
    )

    context = cli._media_work_context(single_stream, video_plan)

    assert context.steps == ("Download video", "Finalize")


def test_media_work_context_lists_enabled_postprocessing(tmp_path: Path) -> None:
    audio = AudioDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        embed_metadata=True,
        write_thumbnail=False,
        embed_thumbnail=True,
    )
    audio_plan = DownloadPlan(
        url=audio.url,
        output_dir=tmp_path,
        outtmpl="%(title)s.%(ext)s",
        format="bestaudio",
        noplaylist=True,
    )

    context = cli._media_work_context(audio, audio_plan)

    assert context.steps == (
        "Download audio",
        "Embed metadata",
        "Embed artwork",
        "Finalize",
    )


def test_video_download_summary_hides_intermediate_stream_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_hook = object()
    monkeypatch.setattr("atlas.planner.which", lambda _name: "/opt/bin/aria2c")

    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(title="Example Video", extractor="youtube")

        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            assert options.dry_run is False
            assert progress_hooks == [expected_hook]
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = [
                "/tmp/Example Video.f298.mp4",
                "/tmp/Example Video.f140.m4a",
                "/tmp/Example Video.mp4",
            ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def hook(self, _event):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli.create_progress_hook", lambda _reporter: expected_hook)

    result = runner.invoke(app, ["video", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert "Saved file:" in result.output
    assert "/tmp/Example Video.mp4" in result.output
    assert "Example Video.f298.mp4" not in result.output
    assert "Example Video.f140.m4a" not in result.output
    assert "Technical files hidden: 2 stream file(s) merged." in result.output


def test_video_download_without_saved_path_preserves_archive_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_hook = object()
    calls: list[bool] = []

    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(title="Example Video", extractor="youtube")

        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            assert progress_hooks == [expected_hook]
            calls.append(options.archive)
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli.create_progress_hook", lambda _reporter: expected_hook)

    result = runner.invoke(app, ["video", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert calls == [True]
    assert "No new file was downloaded" in result.output
    assert "already be recorded in the download archive" in result.output


def test_media_archive_is_private_before_backend_execution(tmp_path: Path) -> None:
    archive = tmp_path / "download-archive.txt"
    archive.write_text("history\n", encoding="utf-8")
    archive.chmod(0o666)
    options = VideoDownloadOptions(
        url="https://example.com/watch?v=1",
        output_dir=tmp_path,
        archive=True,
        archive_file=archive,
    )

    cli._prepare_media_archive(options)

    assert archive.read_text(encoding="utf-8") == "history\n"
    assert archive.stat().st_mode & 0o777 == 0o600


def test_video_overwrite_disables_archive_for_redownload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_hook = object()

    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(title="Example Video", extractor="youtube")

        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            assert options.overwrite is True
            assert options.archive is False
            assert options.archive_file is None
            assert progress_hooks == [expected_hook]
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = ["/tmp/Example Video.mkv"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli.create_progress_hook", lambda _reporter: expected_hook)

    result = runner.invoke(app, ["video", "https://example.com/watch?v=1", "--overwrite"])

    assert result.exit_code == 0
    assert "Download complete" in result.output


def test_audio_download_prints_summary_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_hook = object()

    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(
                id="abc123",
                title="Example Audio",
                uploader="Example Channel",
                extractor="youtube",
                upload_date="20260607",
            )

        def download_audio(self, options, progress_hooks=None, postprocessor_hooks=None):
            assert options.dry_run is False
            assert progress_hooks == [expected_hook]
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.saved_paths = ["/tmp/Example Audio.opus"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def hook(self, _event):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli.create_progress_hook", lambda _reporter: expected_hook)

    result = runner.invoke(app, ["audio", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert "Audio Extraction" in result.output
    assert "Example Audio" in result.output
    assert "YouTube" in result.output
    assert "Audio saved" in result.output


def test_audio_download_without_saved_path_preserves_archive_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_hook = object()
    calls: list[bool] = []

    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(title="Example Audio", extractor="youtube")

        def download_audio(self, options, progress_hooks=None, postprocessor_hooks=None):
            assert progress_hooks == [expected_hook]
            calls.append(options.archive)
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="done",
            )

    class FakeReporter:
        instances = 0

        def __init__(self, _console, **_kwargs):
            type(self).instances += 1
            self.saved_paths = [] if type(self).instances == 1 else ["/tmp/Example Audio.opus"]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.RichProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli.create_progress_hook", lambda _reporter: expected_hook)

    result = runner.invoke(app, ["audio", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert calls == [True]
    assert "No new file was downloaded" in result.output
    assert "--no-archive" in result.output


def test_playlist_video_dry_run_uses_playlist_mode() -> None:
    result = runner.invoke(
        app,
        [
            "playlist",
            "https://www.youtube.com/playlist?list=PL123",
            "--type",
            "video",
            "--video-codec",
            "h264",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"noplaylist": false' in result.output
    assert "[vcodec^=avc1]" in result.output
    assert "%(playlist_title|playlist)s" in result.output


def test_playlist_prompt_can_choose_audio() -> None:
    result = runner.invoke(
        app,
        ["playlist", "https://www.youtube.com/playlist?list=PL123", "--dry-run"],
        input="audio\n",
    )

    assert result.exit_code == 0
    assert "Download playlist as video or audio?" in result.output
    assert "FFmpegExtractAudio" in result.output


def test_playlist_type_contract_only_accepts_video_or_audio() -> None:
    result = runner.invoke(
        app,
        [
            "playlist",
            "https://www.youtube.com/playlist?list=PL123",
            "--type",
            "file",
            "--dry-run",
        ],
    )

    assert result.exit_code == 2
    assert "video" in result.output
    assert "audio" in result.output
    assert "file" in result.output


def test_playlist_refuses_watch_url_with_radio_list() -> None:
    result = runner.invoke(
        app,
        [
            "playlist",
            "https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1",
            "--type",
            "audio",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "only accepts explicit playlist URLs" in result.output
    assert "Traceback" not in result.output


def test_info_uses_engine_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(
                title="Example",
                uploader="Uploader",
                duration=62,
                webpage_url="https://example.com/watch?v=1",
                extractor="youtube",
                upload_date="20260607",
                view_count=123,
                availability="public",
                best_video="18 mp4 360p",
                best_audio="140 m4a 128k",
            )

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["info", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert "Example" in result.output
    assert "Best available" in result.output


def test_info_json_uses_engine_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def get_info(self, _options):
            return MediaInfo(title="Example")

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["info", "https://example.com/watch?v=1", "--json"])

    assert result.exit_code == 0
    assert '"title": "Example"' in result.output


def test_info_rejects_unbounded_channel_without_traceback() -> None:
    result = runner.invoke(
        app,
        ["info", "https://www.youtube.com/@example/videos", "--playlist"],
    )

    assert result.exit_code == 1
    assert "finite selection bound" in result.output
    assert "Traceback" not in result.output


def test_info_accepts_cookie_file_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cookie_file = tmp_path / "cookies.txt"

    class FakeEngine:
        def get_info(self, options):
            assert options.cookies_file == cookie_file
            return MediaInfo(title="Example")

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(
        app,
        ["info", "https://example.com/watch?v=1", "--cookies-file", str(cookie_file)],
    )

    assert result.exit_code == 0
    assert "Example" in result.output


def test_formats_uses_engine_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def list_formats(self, _options):
            return [
                FormatInfo(
                    format_id="18",
                    ext="mp4",
                    resolution="320x240",
                    fps=30,
                    vcodec="avc1",
                    acodec="mp4a",
                    filesize=1024,
                    tbr=250,
                    protocol="https",
                    note="240p",
                )
            ]

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["formats", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert "Available formats" in result.output
    assert "Recommended" in result.output
    assert "18" in result.output


def test_formats_shows_smart_codec_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def list_formats(self, _options):
            return [
                FormatInfo(
                    format_id="401",
                    ext="mp4",
                    resolution="3840x2160",
                    fps=24,
                    vcodec="av01.0.12M.08",
                    acodec="none",
                    filesize=1_500_000_000,
                    tbr=4100,
                ),
                FormatInfo(
                    format_id="313",
                    ext="webm",
                    resolution="3840x2160",
                    fps=24,
                    vcodec="vp9",
                    acodec="none",
                    filesize=2_900_000_000,
                    tbr=8200,
                ),
                FormatInfo(
                    format_id="137",
                    ext="mp4",
                    resolution="1920x1080",
                    fps=24,
                    vcodec="avc1.640028",
                    acodec="none",
                    filesize=590_000_000,
                    tbr=1600,
                ),
                FormatInfo(
                    format_id="251",
                    ext="webm",
                    resolution="audio only",
                    vcodec="none",
                    acodec="opus",
                    filesize=42_000_000,
                    tbr=118,
                ),
            ]

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["formats", "https://example.com/watch?v=1"])

    assert result.exit_code == 0
    assert "Recommended profiles" in result.output
    assert "Best quality" in result.output
    assert "Balanced" in result.output
    assert "Apple compatible" in result.output
    assert "401+251" in result.output
    assert "137+251" in result.output
    assert "1080p" in result.output


def test_smart_format_choices_follow_selected_theme_styles() -> None:
    formats = [
        FormatInfo(
            format_id="401",
            ext="mp4",
            resolution="3840x2160",
            fps=24,
            vcodec="av01.0.12M.08",
            acodec="none",
            filesize=1_500_000_000,
        ),
        FormatInfo(
            format_id="251",
            ext="webm",
            resolution="audio only",
            vcodec="none",
            acodec="opus",
            filesize=42_000_000,
        ),
    ]
    output = StringIO()
    previous_console = cli.console
    try:
        configure_visuals(theme=AtlasThemeName.light, color=True, unicode=True, env={})
        cli.console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            no_color=False,
            theme=Theme(resolve_theme(AtlasThemeName.light)),
        )

        cli._print_smart_format_choices(formats)

        rendered = output.getvalue()
        assert "\x1b[1;34m" in rendered
        assert "Recommended profiles" in rendered
        assert "\x1b[1;36mRecommended profiles" not in rendered
    finally:
        cli.console = previous_console
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_format_summary_lines_use_semantic_text_without_markup_parsing() -> None:
    summary = cli._format_summary_line("Video", "[bold]401[/bold] 2160p av1")
    recommendation = cli._recommended_format_line("bv*+ba/b", "[red]401[/red] + 251")

    assert summary.plain == "Video  [bold]401[/bold] 2160p av1"
    assert recommendation.plain.endswith("[red]401[/red] + 251")
    summary_styles = {str(span.style) for span in summary.spans}
    recommendation_styles = {str(span.style) for span in recommendation.spans}
    assert ATLAS_MUTED_STYLE in summary_styles
    assert ATLAS_MUTED_STYLE in recommendation_styles
    assert ATLAS_ACTIVE_STYLE in recommendation_styles


def test_formats_json_uses_engine_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def list_formats(self, _options):
            return [FormatInfo(format_id="18", ext="mp4")]

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["formats", "https://example.com/watch?v=1", "--json"])

    assert result.exit_code == 0
    assert '"format_id": "18"' in result.output


def test_formats_json_honors_filters_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        def list_formats(self, _options):
            return [
                FormatInfo(format_id="401", ext="mp4", vcodec="av01", acodec="none"),
                FormatInfo(format_id="251", ext="webm", vcodec="none", acodec="opus"),
            ]

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(
        app,
        ["formats", "https://example.com/watch?v=1", "--audio-only", "--json"],
    )

    assert result.exit_code == 0
    assert '"format_id": "251"' in result.output
    assert '"format_id": "401"' not in result.output


def test_formats_rejects_conflicting_filters() -> None:
    result = runner.invoke(
        app,
        ["formats", "https://example.com/watch?v=1", "--video-only", "--audio-only"],
    )

    assert result.exit_code == 1
    assert "Choose either --video-only or --audio-only" in result.output
    assert "Traceback" not in result.output


def test_formats_json_error_is_one_machine_readable_document() -> None:
    result = runner.invoke(
        app,
        [
            "formats",
            "https://example.com/watch?v=1",
            "--video-only",
            "--audio-only",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "AtlasError"


def test_file_json_validation_error_is_one_machine_readable_document() -> None:
    result = runner.invoke(app, ["file", "relative/path", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "ValidationError"
    assert "absolute HTTP or HTTPS" in payload["error"]["message"]


def test_formats_verbose_conflict_keeps_the_original_error() -> None:
    result = runner.invoke(
        app,
        ["formats", "https://example.com/watch?v=1", "--video-only", "--audio-only", "--verbose"],
    )

    assert result.exit_code == 1
    assert "Choose either --video-only or --audio-only" in result.output
    assert "Value for 'trace'" not in result.output


def test_backend_json_nonzero_emits_one_result_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli.run_backend_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=7,
            stdout="",
            stderr="backend rejected the option",
        ),
    )

    result = runner.invoke(app, ["ytdlp", "--json", "--verbose", "--", "--bad-option"])

    assert result.exit_code == 7
    payload = json.loads(result.output)
    assert payload["returncode"] == 7
    assert payload["stderr"] == "backend rejected the option"


def test_audio_preflight_reports_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atlas.cli.ensure_download_dependencies",
        lambda _settings, _kind, _plan: (_ for _ in ()).throw(
            AtlasError("ffmpeg is required for audio extraction.")
        ),
    )

    class FakeEngine:
        def get_info(self, _options):
            msg = "preflight should run before metadata extraction"
            raise AssertionError(msg)

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["audio", "https://example.com/watch?v=1"])

    assert result.exit_code == 1
    assert "ffmpeg is required for audio extraction." in result.output
    assert "Traceback" not in result.output


def test_validation_errors_are_friendly() -> None:
    result = runner.invoke(
        app,
        [
            "video",
            "https://example.com/watch?v=1",
            "--playlist-start",
            "10",
            "--playlist-end",
            "2",
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "playlist_start cannot be greater than playlist_end" in result.output
    assert "Traceback" not in result.output


def test_error_renderer_uses_high_contrast_semantic_styles() -> None:
    output = StringIO()
    previous_console = cli.console
    try:
        configure_visuals(theme=AtlasThemeName.high_contrast, color=True, unicode=True, env={})
        cli.console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            no_color=False,
            theme=Theme(resolve_theme(AtlasThemeName.high_contrast)),
        )

        cli._handle_error(
            AtlasError("aria2c is not installed"),
            verbose=False,
        )

        rendered = output.getvalue()
        assert "\x1b[1;91mError:" in rendered
        assert "\x1b[1;93mHint:" in rendered
        assert "\x1b[31mError:" not in rendered
        assert "\x1b[33mHint:" not in rendered
    finally:
        cli.console = previous_console
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_verbose_error_trace_omits_secret_values() -> None:
    secret = "sentinel-secret"

    with cli.console.capture() as capture:
        try:
            raise AtlasError(f"request failed Authorization: Bearer {secret}")
        except AtlasError as exc:
            cli._handle_error(exc, verbose=True)

    output = capture.get()
    assert secret not in output
    assert "<redacted>" in output
    assert "Trace (values omitted):" in output


def test_machine_json_is_unstyled_and_unwrapped_on_narrow_tty() -> None:
    output = StringIO()
    previous_console = cli.console
    try:
        cli.console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=10,
        )
        cli._print_json({"message": "literal [markup] text that must not wrap"})
    finally:
        cli.console = previous_console

    rendered = output.getvalue()
    assert "\x1b[" not in rendered
    assert json.loads(rendered)["message"] == "literal [markup] text that must not wrap"


def test_file_json_progress_emits_only_parseable_ndjson(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    previous_console = cli.console
    destination = tmp_path / "archive.bin"

    class FakeEngine:
        def plan(self, _options: FileDownloadOptions) -> SimpleNamespace:
            return SimpleNamespace(backend="native", output=destination)

    class FakeAdapter:
        def run(self, options: FileDownloadOptions, progress_callback=None) -> DownloadResult:
            assert progress_callback is not None
            progress_callback(
                ProgressEvent(
                    engine=EngineKind.native,
                    status="starting",
                    phase=ProgressPhase.download,
                    filename=destination.name,
                    url=options.url,
                )
            )
            progress_callback(
                ProgressEvent(
                    engine=EngineKind.native,
                    status="done",
                    phase=ProgressPhase.done,
                    filename=str(destination),
                    url=options.url,
                )
            )
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message=f"Saved to {destination}",
            )

    monkeypatch.setattr("atlas.cli.FileDownloadEngine", FakeEngine)
    monkeypatch.setattr("atlas.cli.DirectFileAdapter", FakeAdapter)
    try:
        cli.console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            width=20,
        )
        paths = cli._run_file_download(
            AtlasSettings(output_dir=tmp_path, archive_file=tmp_path / "archive.txt"),
            FileDownloadOptions(
                url="https://example.com/archive.bin",
                output_dir=tmp_path,
                progress_mode=cli.ProgressMode.json,
            ),
        )
    finally:
        cli.console = previous_console

    lines = output.getvalue().splitlines()
    assert paths == [destination]
    assert [json.loads(line)["status"] for line in lines] == ["starting", "done"]
    assert all("\x1b[" not in line for line in lines)


@pytest.mark.parametrize(
    "args",
    [
        [
            "file",
            "https://example.com/archive.zip",
            "--backend",
            "native",
            "--dry-run",
            "--progress",
            "json",
        ],
        [
            "site",
            "https://example.com/docs/",
            "--backend",
            "wget2",
            "--dry-run",
            "--progress",
            "json",
        ],
    ],
)
def test_single_command_dry_run_progress_json_is_one_ndjson_event(args: list[str]) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 0
    payloads = [json.loads(line) for line in result.output.splitlines() if line]
    assert len(payloads) == 1
    assert payloads[0]["event_type"] == "dry_run"
    assert payloads[0]["status"] == "done"
    assert payloads[0]["exit_code"] == 0
    assert "Dry Run Plan" not in result.output


def test_batch_dry_run_summary(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("# skip\nhttps://example.com/watch?v=1\n", encoding="utf-8")

    result = runner.invoke(app, ["batch", str(batch_file), "--dry-run"])

    assert result.exit_code == 0
    assert "Succeeded" in result.output
    assert "Skipped" in result.output


def test_batch_dry_run_json_progress_is_pure_ndjson(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/archive.zip\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--kind", "file", "--dry-run", "--progress", "json"],
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines
    payloads = [json.loads(line) for line in lines]
    assert payloads[-1]["event_type"] == "batch_summary"
    assert payloads[-1]["status"] == "done"
    assert payloads[-1]["exit_code"] == 0
    assert all("\x1b[" not in line for line in lines)


def test_batch_planning_failure_json_progress_emits_terminal_error(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/archive.zip\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "batch",
            str(batch_file),
            "--kind",
            "file",
            "--backend",
            "bogus",
            "--dry-run",
            "--progress",
            "json",
        ],
    )

    assert result.exit_code == 1
    events = [json.loads(line) for line in result.output.splitlines()]
    assert [event["status"] for event in events] == ["error", "error"]
    assert events[-1]["event_type"] == "batch_summary"
    assert events[-1]["phase"] == "error"
    assert events[-1]["exit_code"] == 1


def test_batch_runtime_json_progress_suppresses_human_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    url = "https://example.com/archive.zip"
    batch_file.write_text(f"{url}\n", encoding="utf-8")

    def fake_shared(*_args: object, **kwargs: object) -> BatchSummary:
        reporter = kwargs["reporter"]
        reporter.hook(
            ProgressEvent(
                engine=EngineKind.aria2,
                status="done",
                phase=ProgressPhase.done,
                line_no=1,
                url=url,
            )
        )
        return BatchSummary(
            kind=BatchKind.file,
            total=1,
            succeeded=1,
            results=[
                BatchItemResult(
                    entry=BatchEntry(line_no=1, url=url),
                    status=DownloadStatus.success,
                    message="Saved",
                )
            ],
        )

    monkeypatch.setattr("atlas.cli._try_run_aria2_batch_queue", fake_shared)

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--kind", "file", "--progress", "json"],
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    payloads = [json.loads(line) for line in lines]
    assert [payload["status"] for payload in payloads] == ["done", "done"]
    assert payloads[-1]["event_type"] == "batch_summary"
    assert payloads[-1]["exit_code"] == 0
    assert "Batch file" not in result.output


def test_file_json_progress_failure_is_pure_ndjson(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "archive.bin"

    class FakeEngine:
        def plan(self, _options: FileDownloadOptions) -> SimpleNamespace:
            return SimpleNamespace(backend="native", output=destination)

    class FailingAdapter:
        def run(self, _options: FileDownloadOptions, progress_callback=None) -> DownloadResult:
            _ = progress_callback
            raise AtlasError("backend exploded")

    monkeypatch.setattr("atlas.cli.FileDownloadEngine", FakeEngine)
    monkeypatch.setattr("atlas.cli.DirectFileAdapter", FailingAdapter)

    result = runner.invoke(
        app,
        ["file", "https://example.com/archive.bin", "--progress", "json"],
    )

    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "error"
    assert payload["message"] == "backend exploded"


def test_batch_adaptive_missing_input_json_has_no_traceback(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "batch",
            str(tmp_path / "missing.txt"),
            "--adaptive",
            "--json",
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert "Traceback" not in result.output


def test_batch_rejects_invalid_artifact_root_before_execution(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/archive.zip\n", encoding="utf-8")
    invalid_output = tmp_path / "not-a-directory"
    invalid_output.write_text("occupied", encoding="utf-8")

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--json", "--output-dir", str(invalid_output)],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert "artifact folder" in payload["error"]["message"]


def test_batch_artifact_failure_preserves_machine_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    url = "https://example.com/archive.zip"
    batch_file.write_text(f"{url}\n", encoding="utf-8")
    summary = BatchSummary(
        kind=BatchKind.file,
        total=1,
        succeeded=1,
        results=[
            BatchItemResult(
                entry=BatchEntry(line_no=1, url=url),
                status=DownloadStatus.success,
                message="Saved",
            )
        ],
    )
    monkeypatch.setattr("atlas.cli._try_run_aria2_batch_queue", lambda *_a, **_kw: summary)

    def fail_artifacts(*_args: object, **_kwargs: object) -> dict[str, Path]:
        raise AtlasError("artifact write failed")

    monkeypatch.setattr("atlas.cli._write_batch_artifacts", fail_artifacts)

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--json", "--output-dir", str(tmp_path / "out")],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["summary"]["succeeded"] == 1


def test_batch_cancellation_uses_nonzero_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    url = "https://example.com/archive.zip"
    batch_file.write_text(f"{url}\n", encoding="utf-8")
    summary = BatchSummary(
        kind=BatchKind.file,
        total=1,
        skipped=1,
        canceled=1,
        results=[
            BatchItemResult(
                entry=BatchEntry(line_no=1, url=url),
                status=DownloadStatus.canceled,
                message="canceled by operator",
            )
        ],
    )
    monkeypatch.setattr("atlas.cli._try_run_aria2_batch_queue", lambda *_a, **_kw: summary)

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--json", "--output-dir", str(tmp_path / "out")],
    )

    assert result.exit_code == 130
    assert json.loads(result.output)["canceled"] == 1


def test_batch_continues_after_failure_and_counts_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("# skip\nhttps://ok.example\nhttps://fail.example\n", encoding="utf-8")

    class FakeEngine:
        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            if "fail" in options.url:
                raise RuntimeError("boom")
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="saved",
            )

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["batch", str(batch_file), "--type", "video"])

    assert result.exit_code == 1
    assert "Succeeded:" in result.output
    assert "1" in result.output
    assert "Failed:" in result.output
    assert "Skipped:" in result.output
    assert "boom" in result.output


def test_batch_summary_text_uses_semantic_styles() -> None:
    summary = BatchSummary(
        kind=BatchKind.auto,
        total=3,
        succeeded=1,
        failed=1,
        skipped=1,
        results=[],
    )

    rendered = cli._batch_summary_text(summary)

    styles = {str(span.style) for span in rendered.spans}
    assert ATLAS_MUTED_STYLE in styles
    assert ATLAS_SUCCESS_STYLE in styles
    assert ATLAS_ERROR_STYLE in styles
    assert ATLAS_WARNING_STYLE in styles


def test_batch_summary_text_hides_zero_problem_counts() -> None:
    summary = BatchSummary(
        kind=BatchKind.auto,
        total=2,
        succeeded=2,
        failed=0,
        skipped=0,
        results=[],
    )

    rendered = cli._batch_summary_text(summary).plain

    assert rendered == "Succeeded: 2"


def test_batch_result_display_message_compacts_success_paths() -> None:
    fallback = BatchItemResult(
        entry=BatchEntry(line_no=1, url="https://example.com/book.epub"),
        status=DownloadStatus.success,
        message="Saved to /tmp/out/book.epub (curl TLS fallback)",
    )
    normal = BatchItemResult(
        entry=BatchEntry(line_no=2, url="https://example.com/file.zip"),
        status=DownloadStatus.success,
        message="Saved to /tmp/out/file.zip",
    )
    failed = BatchItemResult(
        entry=BatchEntry(line_no=3, url="https://example.com/fail.zip"),
        status=DownloadStatus.failed,
        message="curl exited 60",
    )

    assert cli._batch_result_display_message(fallback) == "Saved (curl fallback)"
    assert cli._batch_result_display_message(normal) == "Saved"
    assert cli._batch_result_display_message(failed) == "curl exited 60"


def test_styled_path_uses_semantic_path_style(tmp_path: Path) -> None:
    styled = cli._styled_path(tmp_path / "download.bin")

    assert styled.startswith(f"[{ATLAS_PATH_STYLE}]")
    assert styled.endswith(f"[/{ATLAS_PATH_STYLE}]")
    assert "[dim italic]" not in styled


def test_batch_artifact_panel_uses_selected_theme_styles(tmp_path: Path) -> None:
    output = StringIO()
    previous_console = cli.console
    try:
        configure_visuals(theme=AtlasThemeName.light, color=True, unicode=True, env={})
        panel = cli._artifact_panel({"latest_manifest": tmp_path / "manifest.json"})
        assert isinstance(panel.title, Text)
        assert panel.title.style == ATLAS_TITLE_STYLE
        assert panel.border_style == ATLAS_PANEL_STYLE

        cli.console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            theme=Theme(resolve_theme(AtlasThemeName.light)),
        )

        cli._print_artifact_panel({"latest_manifest": tmp_path / "manifest.json"})

        rendered = output.getvalue()
        assert "Artifacts" in rendered
        assert ATLAS_TITLE_STYLE not in rendered
        assert ATLAS_PANEL_STYLE not in rendered
    finally:
        cli.console = previous_console
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_batch_artifact_panel_hides_empty_retry_and_failure_rows(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    summary = tmp_path / "summary.json"
    retry = tmp_path / "retry.atlas.json"
    failed = tmp_path / "failed.txt"
    skipped = tmp_path / "skipped.txt"
    manifest.write_text("{}", encoding="utf-8")
    summary.write_text("{}", encoding="utf-8")
    retry.write_text(
        json.dumps(
            {
                "retry_failed_only": [],
                "retry_skipped_unknowns_only": [],
                "retry_canceled_only": [],
                "retry_checksum_failures_only": [],
            }
        ),
        encoding="utf-8",
    )
    failed.write_text("", encoding="utf-8")
    skipped.write_text("", encoding="utf-8")

    output = StringIO()
    Console(
        file=output,
        width=120,
        force_terminal=False,
        theme=Theme(resolve_theme(AtlasThemeName.auto)),
    ).print(
        cli._artifact_panel(
            {
                "latest_manifest": manifest,
                "latest_summary": summary,
                "retry_manifest": retry,
                "failed": failed,
                "skipped": skipped,
            }
        )
    )
    rendered = output.getvalue()

    assert "Manifest" in rendered
    assert "Summary" in rendered
    assert "Retry" not in rendered
    assert "Failed URLs" not in rendered
    assert "Skipped URLs" not in rendered


def test_batch_artifact_panel_shows_actionable_retry_rows(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    summary = tmp_path / "summary.json"
    retry = tmp_path / "retry.atlas.json"
    failed = tmp_path / "failed.txt"
    manifest.write_text("{}", encoding="utf-8")
    summary.write_text("{}", encoding="utf-8")
    retry.write_text(
        json.dumps(
            {
                "retry_failed_only": ["https://example.com/bad.iso"],
                "retry_skipped_unknowns_only": [],
                "retry_canceled_only": [],
                "retry_checksum_failures_only": [],
            }
        ),
        encoding="utf-8",
    )
    failed.write_text("https://example.com/bad.iso\n", encoding="utf-8")

    output = StringIO()
    Console(
        file=output,
        width=120,
        force_terminal=False,
        theme=Theme(resolve_theme(AtlasThemeName.auto)),
    ).print(
        cli._artifact_panel(
            {
                "latest_manifest": manifest,
                "latest_summary": summary,
                "retry_manifest": retry,
                "failed": failed,
            }
        )
    )
    rendered = output.getvalue()

    assert "Retry" in rendered
    assert "Failed URLs" in rendered


def test_batch_type_audio_routes_to_audio_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://audio.example\n", encoding="utf-8")
    calls: list[str] = []

    class FakeEngine:
        def download_audio(self, options, progress_hooks=None, postprocessor_hooks=None):
            calls.append(options.url)
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="audio saved",
            )

        def download_video(self, _options, progress_hooks=None, postprocessor_hooks=None):
            raise AssertionError("video engine should not be used")

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())

    result = runner.invoke(app, ["batch", str(batch_file), "--type", "audio"])

    assert result.exit_code == 0
    assert calls == ["https://audio.example"]
    assert "audio" in result.output
    assert "yt-dlp" in result.output


def test_batch_real_file_summary_keeps_route_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/archive.zip\n", encoding="utf-8")

    monkeypatch.setattr(
        "atlas.optimizer.probe_direct_file",
        lambda url: DirectFileProbe(
            url=url,
            final_url=url,
            content_length=1024,
            file_extension=".zip",
        ),
    )

    def fake_run(self, options, *, progress_callback=None):
        return DownloadResult(
            status=DownloadStatus.success,
            url=options.url,
            message="saved",
            ydl_opts={"backend": "native"},
        )

    monkeypatch.setattr("atlas.cli.DirectFileAdapter.run", fake_run)

    result = runner.invoke(
        app,
        [
            "batch",
            str(batch_file),
            "--kind",
            "file",
            "--backend",
            "native",
            "--progress",
            "none",
        ],
    )

    assert result.exit_code == 0
    assert "file" in result.output
    assert "native" in result.output


def test_batch_concurrency_passes_progress_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_hook = object()
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://one.example\nhttps://two.example\n", encoding="utf-8")
    calls: list[str] = []

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            self.seeded = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def seed_entries(self, entries, *, kind=None):
            self.seeded = [(entry.line_no, entry.url, kind) for entry in entries]

    class FakeEngine:
        def download_video(self, options, progress_hooks=None, postprocessor_hooks=None):
            assert progress_hooks is not None
            assert progress_hooks[0] is expected_hook
            assert len(progress_hooks) == 2
            assert callable(progress_hooks[1])
            calls.append(options.url)
            return DownloadResult(
                status=DownloadStatus.success,
                url=options.url,
                message="saved",
            )

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.BatchProgressReporter", FakeReporter)
    monkeypatch.setattr(
        "atlas.cli.create_batch_progress_hook",
        lambda _reporter, *, line_no, url: expected_hook,
    )

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--type", "video", "--concurrency", "2"],
    )

    assert result.exit_code == 0
    assert "concurrency 2" in result.output
    assert sorted(calls) == ["https://one.example", "https://two.example"]


def test_batch_adaptive_uses_runtime_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://one.example/a.txt\nhttps://two.example/b.txt\n",
        encoding="utf-8",
    )
    called: list[tuple[int, int]] = []

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def seed_entries(self, _entries, *, kind=None):
            return None

    def fake_scan_direct_file(url: str, *, dry_run: bool):
        return WorkItem(url=url, host=url.split("/")[2], content_length=1024)

    def fake_run_batch_adaptive(
        file,
        kind,
        handler,
        *,
        scheduler,
        progress_hook_factory=None,
        host_resolver=None,
        control=None,
    ):
        _ = control
        called.append((scheduler.global_max_concurrency, scheduler.per_host_concurrency))
        return BatchSummary(kind=kind, total=2, succeeded=2)

    monkeypatch.setattr("atlas.cli.BatchProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli.scan_direct_file", fake_scan_direct_file)
    monkeypatch.setattr("atlas.cli._try_run_aria2_batch_queue", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("atlas.cli.run_batch_adaptive", fake_run_batch_adaptive)

    result = runner.invoke(
        app,
        [
            "batch",
            str(batch_file),
            "--adaptive",
            "--max-concurrency",
            "8",
            "--per-host-concurrency",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert called == [(2, 1)]


def test_batch_artifacts_write_summary_manifest_and_retry(tmp_path: Path) -> None:
    summary = _retryable_batch_summary()
    plan = AdaptiveDownloadPlan(
        enabled=True,
        queue_concurrency=8,
        per_host_concurrency=2,
        per_file_segments=1,
        max_total_connections=8,
        max_per_host_connections=2,
        backend="native",
        strategy="adaptive small-file lane",
    )

    paths = _write_batch_artifacts(summary, output_dir=tmp_path, adaptive_plan=plan)

    assert set(paths) == {
        "summary",
        "manifest",
        "retry",
        "latest_summary",
        "latest_manifest",
        "failed",
        "skipped",
        "canceled",
        "retry_manifest",
    }
    summary_data = json.loads(paths["summary"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    latest_manifest = json.loads(paths["latest_manifest"].read_text(encoding="utf-8"))
    retry_manifest = json.loads(paths["retry_manifest"].read_text(encoding="utf-8"))
    retry = paths["retry"].read_text(encoding="utf-8").splitlines()
    failed = paths["failed"].read_text(encoding="utf-8").splitlines()
    skipped = paths["skipped"].read_text(encoding="utf-8").splitlines()
    canceled = paths["canceled"].read_text(encoding="utf-8").splitlines()
    assert summary_data["failed"] == 2
    assert summary_data["canceled"] == 0
    assert manifest["adaptive_plan"]["max_total_connections"] == 8
    assert manifest["smart_session"]["session_type"] == "batch_session"
    assert manifest["smart_session"]["scheduler_policy"]["max_total_connections"] == 8
    assert manifest["artifacts"]["canceled"].endswith("canceled.txt")
    assert manifest["items"][1]["url"] == "https://example.com/missing.txt"
    assert manifest["items"][2]["backend_args"] == [
        "/usr/bin/aria2c",
        "--header=Authorization: <redacted>",
        "https://example.com/bad.iso",
    ]
    assert (
        manifest["items"][2]["backend_command"]
        == "/usr/bin/aria2c '--header=Authorization: <redacted>' "
        "https://example.com/bad.iso"
    )
    assert latest_manifest["artifacts"]["retry"].endswith("retry.atlas.json")
    assert retry == ["https://example.com/missing.txt", "https://example.com/bad.iso"]
    assert failed == ["https://example.com/missing.txt", "https://example.com/bad.iso"]
    assert skipped == ["https://example.com/unknown"]
    assert canceled == []
    assert retry_manifest["retry_failed_only"] == failed
    assert Path(retry_manifest["export_failed_urls"]) == paths["retry"]
    assert Path(retry_manifest["export_failed_urls"]).exists()
    assert retry_manifest["retry_checksum_failures_only"] == ["https://example.com/bad.iso"]
    assert retry_manifest["retry_skipped_unknowns_only"] == ["https://example.com/unknown"]
    assert retry_manifest["retry_canceled_only"] == []


def test_batch_artifacts_use_unique_generations_and_reject_latest_symlinks(
    tmp_path: Path,
) -> None:
    first = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    second = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    assert first["summary"] != second["summary"]
    assert first["manifest"] != second["manifest"]
    assert json.loads(second["latest_manifest"].read_text(encoding="utf-8"))["items"]

    latest = tmp_path / ".atlas" / "latest"
    victim = tmp_path / "victim"
    victim.mkdir()
    victim_manifest = victim / "manifest.json"
    victim_manifest.write_text("untouched", encoding="utf-8")
    (latest / "manifest.json").unlink()
    (latest / "manifest.json").symlink_to(victim_manifest)
    recovered = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    assert victim_manifest.read_text(encoding="utf-8") == "untouched"
    assert not recovered["latest_manifest"].is_symlink()

    shutil.rmtree(latest)
    latest.symlink_to(victim, target_is_directory=True)

    with pytest.raises(AtlasError, match="symbolic-link latest"):
        _write_batch_artifacts(
            _retryable_batch_summary(),
            output_dir=tmp_path,
            adaptive_plan=None,
        )
    assert victim_manifest.read_text(encoding="utf-8") == "untouched"


def _retryable_batch_summary() -> BatchSummary:
    return BatchSummary(
        kind=BatchKind.file,
        total=4,
        succeeded=1,
        failed=2,
        skipped=1,
        results=[
            BatchItemResult(
                entry=BatchEntry(line_no=1, url="https://example.com/ok.txt"),
                status=DownloadStatus.success,
                message="Saved",
                plan={"route": {"kind": "file", "engine": "native"}},
            ),
            BatchItemResult(
                entry=BatchEntry(line_no=2, url="https://example.com/missing.txt"),
                status=DownloadStatus.failed,
                message="404",
                plan={"route": {"kind": "file", "engine": "native"}},
            ),
            BatchItemResult(
                entry=BatchEntry(line_no=3, url="https://example.com/bad.iso"),
                status=DownloadStatus.failed,
                message="checksum mismatch",
                plan={
                    "route": {"kind": "file", "engine": "aria2"},
                    "args": [
                        "/usr/bin/aria2c",
                        "--header=Authorization: <redacted>",
                        "https://example.com/bad.iso",
                    ],
                },
            ),
            BatchItemResult(
                entry=BatchEntry(line_no=4, url="https://example.com/unknown"),
                status=DownloadStatus.skipped,
                message="unknown route",
                plan={"route": {"kind": "unknown", "engine": "none"}},
            ),
        ],
    )


def _canceled_batch_summary() -> BatchSummary:
    return BatchSummary(
        kind=BatchKind.file,
        total=2,
        succeeded=1,
        skipped=1,
        canceled=1,
        results=[
            BatchItemResult(
                entry=BatchEntry(line_no=1, url="https://example.com/ok.txt"),
                status=DownloadStatus.success,
                message="Saved",
                plan={"route": {"kind": "file", "engine": "native"}},
            ),
            BatchItemResult(
                entry=BatchEntry(line_no=2, url="https://example.com/canceled.txt"),
                status=DownloadStatus.canceled,
                message="canceled by operator",
                plan={"route": {"kind": "file", "engine": "native"}},
            ),
        ],
    )


def test_retry_command_runs_checksum_failures_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    calls: list[dict[str, object]] = []

    def fake_batch(file: Path, **kwargs: object) -> None:
        calls.append({"file": file, "kwargs": kwargs, "urls": file.read_text(encoding="utf-8")})

    monkeypatch.setattr("atlas.cli.batch", fake_batch)

    result = runner.invoke(
        app,
        [
            "retry",
            str(paths["retry_manifest"]),
            "--checksum-failures-only",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["urls"] == "https://example.com/bad.iso\n"
    kwargs = calls[0]["kwargs"]
    assert kwargs["kind"] == BatchKind.file
    assert kwargs["dry_run"] is True
    assert kwargs["output_dir"] == tmp_path
    assert not (tmp_path / ".atlas" / "retry").exists()


def test_retry_batch_files_are_unique_private_and_reject_symlink_dirs(
    tmp_path: Path,
) -> None:
    first = cli._write_retry_batch_file(
        ["https://example.com/one"],
        output_dir=tmp_path,
        mode="failed",
    )
    second = cli._write_retry_batch_file(
        ["https://example.com/two"],
        output_dir=tmp_path,
        mode="failed",
    )

    assert first != second
    assert first.stat().st_mode & 0o777 == 0o600
    assert first.parent.stat().st_mode & 0o777 == 0o700

    unsafe_output = tmp_path / "unsafe"
    outside = tmp_path / "outside"
    (unsafe_output / ".atlas").mkdir(parents=True)
    outside.mkdir()
    (unsafe_output / ".atlas" / "retry").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AtlasError, match="private retry folder"):
        cli._write_retry_batch_file(
            ["https://example.com/secret"],
            output_dir=unsafe_output,
            mode="failed",
        )

    assert list(outside.iterdir()) == []


def test_retry_command_runs_canceled_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_batch_artifacts(
        _canceled_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    calls: list[str] = []

    def fake_batch(file: Path, **_kwargs: object) -> None:
        calls.append(file.read_text(encoding="utf-8"))

    monkeypatch.setattr("atlas.cli.batch", fake_batch)

    result = runner.invoke(
        app,
        [
            "retry",
            str(paths["retry_manifest"]),
            "--canceled-only",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert calls == ["https://example.com/canceled.txt\n"]


def test_resume_command_uses_failed_and_skipped_unknown_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_batch_artifacts(_retryable_batch_summary(), output_dir=tmp_path, adaptive_plan=None)
    calls: list[str] = []

    def fake_batch(file: Path, **_kwargs: object) -> None:
        calls.append(file.read_text(encoding="utf-8"))

    monkeypatch.setattr("atlas.cli.batch", fake_batch)

    result = runner.invoke(app, ["resume", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert calls == [
        (
            "https://example.com/missing.txt\n"
            "https://example.com/bad.iso\n"
            "https://example.com/unknown\n"
        )
    ]


def test_resume_command_includes_canceled_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_batch_artifacts(_canceled_batch_summary(), output_dir=tmp_path, adaptive_plan=None)
    calls: list[str] = []

    def fake_batch(file: Path, **_kwargs: object) -> None:
        calls.append(file.read_text(encoding="utf-8"))

    monkeypatch.setattr("atlas.cli.batch", fake_batch)

    result = runner.invoke(app, ["resume", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert calls == ["https://example.com/canceled.txt\n"]


def test_export_failed_command_can_export_canceled_urls(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _canceled_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        [
            "export-failed",
            str(paths["latest_manifest"]),
            "--canceled-only",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["mode"] == "canceled"
    assert payload["urls"] == ["https://example.com/canceled.txt"]


def test_export_failed_command_reads_manifest_json(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    output = tmp_path / "failed-export.txt"

    result = runner.invoke(
        app,
        [
            "export-failed",
            str(paths["latest_manifest"]),
            "--output",
            str(output),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "https://example.com/missing.txt",
        "https://example.com/bad.iso",
    ]
    payload = json.loads(result.output)
    assert payload["count"] == 2


def test_export_failed_refuses_existing_or_session_alias_output(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    output = tmp_path / "failed-export.txt"
    output.write_text("keep this\n", encoding="utf-8")

    existing = runner.invoke(
        app,
        ["export-failed", str(paths["latest_manifest"]), "--output", str(output)],
    )
    aliased = runner.invoke(
        app,
        [
            "export-failed",
            str(paths["latest_manifest"]),
            "--output",
            str(paths["latest_manifest"]),
            "--force",
        ],
    )
    forced = runner.invoke(
        app,
        [
            "export-failed",
            str(paths["latest_manifest"]),
            "--output",
            str(output),
            "--force",
        ],
    )

    assert existing.exit_code == 1
    assert "--force" in existing.output
    assert aliased.exit_code == 1
    assert json.loads(paths["latest_manifest"].read_text(encoding="utf-8"))["items"]
    assert forced.exit_code == 0
    assert output.read_text(encoding="utf-8").splitlines() == [
        "https://example.com/missing.txt",
        "https://example.com/bad.iso",
    ]


def test_inspect_session_command_renders_operator_panels(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(app, ["inspect-session", str(paths["retry_manifest"])])

    assert result.exit_code == 0
    assert "Saved Session" in result.output
    assert "Operator Actions" in result.output
    assert "atlas retry" in result.output
    assert "Failures" in result.output
    assert "Line 2" in result.output
    assert "checksum mismatch" in result.output


def test_inspect_session_rejects_manifest_outside_session_trust_root(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "session"
    outside_dir = tmp_path / "outside"
    session_dir.mkdir()
    outside_dir.mkdir()
    outside_manifest = outside_dir / "manifest.json"
    outside_manifest.write_text('{"items": []}\n', encoding="utf-8")
    retry_manifest = session_dir / "retry.atlas.json"
    retry_manifest.write_text(
        json.dumps(
            {
                "kind": "file",
                "manifest_path": str(outside_manifest),
                "retry_failed_only": [],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["inspect-session", str(retry_manifest)])

    assert result.exit_code == 1
    assert "outside its trusted session folder" in result.output


def test_inspect_session_ignores_untrusted_saved_output_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "safe-output"
    latest_dir = output_root / ".atlas" / "latest"
    latest_dir.mkdir(parents=True)
    untrusted_output = tmp_path / "untrusted-output"
    retry_manifest = latest_dir / "retry.atlas.json"
    retry_manifest.write_text(
        json.dumps(
            {
                "kind": "file",
                "retry_failed_only": [],
                "smart_session": {"customization": {"output_dir": str(untrusted_output)}},
            }
        ),
        encoding="utf-8",
    )
    opened: list[list[str]] = []
    monkeypatch.setattr(
        "atlas.cli.shutil.which",
        lambda name: "/usr/bin/open" if name == "open" else None,
    )
    monkeypatch.setattr(
        "atlas.cli.subprocess.run",
        lambda args, **_kwargs: opened.append(args),
    )

    result = runner.invoke(
        app,
        ["inspect-session", str(retry_manifest), "--open-output", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["output_dir"] == str(output_root)
    assert opened == [["/usr/bin/open", str(output_root)]]
    assert not untrusted_output.exists()


def test_inspect_session_command_json_is_machine_stable(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        ["inspect-session", str(paths["latest_manifest"]), "--json", "--item", "3"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["kind"] == "file"
    assert payload["counts"]["failed"] == 2
    assert payload["retry"]["failed"]["count"] == 2
    assert payload["retry"]["checksum"]["urls"] == ["https://example.com/bad.iso"]
    assert payload["backend_commands"]["total"] == 1
    assert payload["backend_commands"]["sample"][0]["line_no"] == 3
    assert payload["item"]["url"] == "https://example.com/bad.iso"
    assert "\x1b[" not in result.output


def test_inspect_session_command_filters_json_items(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--json",
            "--status",
            "failed",
            "--filter",
            "checksum",
            "--kind-filter",
            "file",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["filters"] == {
        "query": "checksum",
        "status": "failed",
        "kind": "file",
        "matched": 1,
        "total": 4,
    }
    assert payload["items"]["total"] == 4
    assert payload["items"]["matched"] == 1
    assert payload["items"]["sample"][0]["line_no"] == 3
    assert payload["items"]["sample"][0]["url"] == "https://example.com/bad.iso"
    assert "\x1b[" not in result.output


def test_inspect_session_command_json_includes_focused_panel(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        ["inspect-session", str(paths["retry_manifest"]), "--panel", "failed", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["panel"]["selected"] == "failed"
    assert payload["panel"]["total"] == 2
    assert [item["line_no"] for item in payload["panel"]["sample"]] == [2, 3]
    assert "\x1b[" not in result.output


def test_inspect_session_command_human_scheduler_panel(tmp_path: Path) -> None:
    plan = AdaptiveDownloadPlan(
        enabled=True,
        queue_concurrency=8,
        per_host_concurrency=2,
        per_file_segments=1,
        max_total_connections=8,
        max_per_host_connections=2,
        backend="native",
        strategy="adaptive small-file lane",
    )
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=plan,
    )

    result = runner.invoke(
        app,
        ["inspect-session", str(paths["retry_manifest"]), "--panel", "scheduler"],
    )

    assert result.exit_code == 0
    assert "Panels" in result.output
    assert "[scheduler]" in result.output
    assert "Queue Concurrency" in result.output
    assert "adaptive small-file lane" in result.output


def test_inspect_session_command_previews_focused_plan(tmp_path: Path) -> None:
    plan = AdaptiveDownloadPlan(
        enabled=True,
        queue_concurrency=8,
        per_host_concurrency=2,
        per_file_segments=1,
        max_total_connections=8,
        max_per_host_connections=2,
        backend="native",
        strategy="adaptive small-file lane",
    )
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=plan,
    )

    result = runner.invoke(
        app,
        ["inspect-session", str(paths["retry_manifest"]), "--preview", "plan"],
    )

    assert result.exit_code == 0
    assert "Plan JSON" in result.output
    assert '"adaptive_plan"' in result.output
    assert '"queue_concurrency": 8' in result.output
    assert '"scheduler"' in result.output
    assert '"smart_session"' in result.output


def test_inspect_session_command_previews_backend_commands(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--preview",
            "backend",
            "--filter",
            "bad.iso",
        ],
    )

    assert result.exit_code == 0
    assert "Backend Commands" in result.output
    assert "/usr/bin/aria2c" in result.output
    assert "Authorization: <redacted>" in result.output
    assert "missing.txt" not in result.output


def test_inspect_session_command_filters_human_rows(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--status",
            "failed",
            "--filter",
            "checksum",
        ],
    )

    assert result.exit_code == 0
    assert "Filter" in result.output
    assert "status=failed" in result.output
    assert "query=checksum" in result.output
    assert "1/4 matched" in result.output
    assert "checksum mismatch" in result.output
    assert "Line 3" in result.output
    assert "Line 2" not in result.output


def test_inspect_session_command_copies_operator_command_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    copied: list[str] = []

    monkeypatch.setattr(
        "atlas.cli.shutil.which",
        lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None,
    )

    def fake_run(
        args: list[str],
        *,
        input: str | None = None,
        text: bool = False,
        check: bool = False,
    ) -> None:
        assert args == ["/usr/bin/pbcopy"]
        assert text is True
        assert check is False
        copied.append(input or "")

    monkeypatch.setattr("atlas.cli.subprocess.run", fake_run)

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--copy-command",
            "resume",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["actions"]["copied"] is True
    assert payload["actions"]["copied_command"].startswith("atlas resume ")
    assert copied == [payload["actions"]["copied_command"]]
    assert "\x1b[" not in result.output


def test_inspect_session_command_copies_backend_command_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    copied: list[str] = []

    monkeypatch.setattr(
        "atlas.cli.shutil.which",
        lambda name: "/usr/bin/pbcopy" if name == "pbcopy" else None,
    )

    def fake_run(
        args: list[str],
        *,
        input: str | None = None,
        text: bool = False,
        check: bool = False,
    ) -> None:
        assert args == ["/usr/bin/pbcopy"]
        assert text is True
        assert check is False
        copied.append(input or "")

    monkeypatch.setattr("atlas.cli.subprocess.run", fake_run)

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--filter",
            "bad.iso",
            "--copy-command",
            "backend",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["actions"]["copied"] is True
    assert payload["actions"]["copied_command"] == copied[0]
    assert payload["actions"]["copied_command"].startswith("/usr/bin/aria2c")
    assert "Authorization: <redacted>" in payload["actions"]["copied_command"]
    assert "\x1b[" not in result.output


def test_inspect_session_command_reports_unavailable_clipboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    monkeypatch.setattr("atlas.cli.shutil.which", lambda _name: None)

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--copy-command",
            "export-failed",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["actions"]["copied"] is False
    assert payload["actions"]["copied_command"].startswith("atlas export-failed ")


def test_inspect_session_command_opens_output_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    opened: list[list[str]] = []

    monkeypatch.setattr(
        "atlas.cli.shutil.which",
        lambda name: "/usr/bin/open" if name == "open" else None,
    )

    def fake_run(args: list[str], *, check: bool = False, **_kwargs: object) -> None:
        assert check is False
        opened.append(args)

    monkeypatch.setattr("atlas.cli.subprocess.run", fake_run)

    result = runner.invoke(
        app,
        ["inspect-session", str(paths["retry_manifest"]), "--open-output", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["actions"]["opened_output"] is True
    assert opened == [["/usr/bin/open", str(tmp_path)]]


def test_inspect_session_command_exports_filtered_urls(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    export_path = tmp_path / "exports" / "checksum.txt"

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--status",
            "failed",
            "--filter",
            "checksum",
            "--export-urls",
            str(export_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert export_path.read_text(encoding="utf-8") == "https://example.com/bad.iso\n"
    payload = json.loads(result.output)
    assert payload["actions"]["exported_urls"] == str(export_path)
    assert payload["actions"]["exported_count"] == 1
    assert payload["items"]["matched"] == 1


def test_inspect_session_command_plain_preview_is_ascii(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        [
            "--plain",
            "--no-unicode",
            "inspect-session",
            str(paths["retry_manifest"]),
            "--preview",
            "failed",
            "--limit",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "Failed URLs" in result.output
    assert "https://example.com/bad.iso" in result.output
    assert "\x1b[" not in result.output
    assert "█" not in result.output
    assert "░" not in result.output


def test_inspect_session_command_previews_filtered_errors(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        [
            "inspect-session",
            str(paths["retry_manifest"]),
            "--preview",
            "errors",
            "--filter",
            "checksum",
            "--status",
            "failed",
        ],
    )

    assert result.exit_code == 0
    assert "Error Report JSON" in result.output
    assert '"matched": 1' in result.output
    assert '"total": 2' in result.output
    assert "checksum mismatch" in result.output
    assert "missing.txt" not in result.output


def test_inspect_session_command_previews_log_and_config_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )
    log_root = tmp_path / "logs"
    log_root.mkdir()
    (log_root / "atlas.log").write_text("2026-06-09 INFO atlas: clean\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text("output_dir = '~/Downloads/atlas'\n", encoding="utf-8")
    monkeypatch.setattr("atlas.cli.log_dir", lambda: log_root)
    monkeypatch.setattr("atlas.cli.config_path", lambda: config)

    log_result = runner.invoke(
        app,
        ["inspect-session", str(paths["retry_manifest"]), "--preview", "logs"],
    )
    config_result = runner.invoke(
        app,
        ["inspect-session", str(paths["retry_manifest"]), "--preview", "config"],
    )

    assert log_result.exit_code == 0
    assert "Atlas Log" in log_result.output
    assert "clean" in log_result.output
    assert config_result.exit_code == 0
    assert "Atlas Config" in config_result.output
    assert "output_dir" in config_result.output


def test_retry_command_rejects_conflicting_selectors(tmp_path: Path) -> None:
    paths = _write_batch_artifacts(
        _retryable_batch_summary(),
        output_dir=tmp_path,
        adaptive_plan=None,
    )

    result = runner.invoke(
        app,
        [
            "retry",
            str(paths["retry_manifest"]),
            "--failed-only",
            "--checksum-failures-only",
        ],
    )

    assert result.exit_code == 1
    assert "Choose only one retry selector" in result.output


def test_mirror_artifacts_write_stable_session_files(tmp_path: Path) -> None:
    options = SiteDownloadOptions(url="https://example.com/docs/", output_dir=tmp_path)
    result = DownloadResult(
        status=DownloadStatus.success,
        url=options.url,
        message=f"Saved under {tmp_path}",
        ydl_opts={
            "stats": {
                "site": {
                    "rows": [
                        {"url": "https://example.com/docs/", "status": "200"},
                        {"url": "https://example.com/docs/missing.html", "status": "404"},
                    ]
                },
                "summary": {"site": {"failures": 1}},
            }
        },
    )

    paths = _write_mirror_artifacts(options, result, backend="wget2")

    assert set(paths) == {
        "latest_manifest",
        "latest_summary",
        "failed",
        "skipped",
        "canceled",
        "retry_manifest",
    }
    manifest = json.loads(paths["latest_manifest"].read_text(encoding="utf-8"))
    summary = json.loads(paths["latest_summary"].read_text(encoding="utf-8"))
    retry_manifest = json.loads(paths["retry_manifest"].read_text(encoding="utf-8"))
    assert manifest["smart_session"]["session_type"] == "site_session"
    assert manifest["artifacts"]["retry"].endswith("retry.atlas.json")
    assert summary["failed"] == 0
    assert summary["failed_resource_count"] == 1
    assert summary["failed_resource_urls"] == ["https://example.com/docs/missing.html"]
    assert paths["failed"].read_text(encoding="utf-8").splitlines() == [options.url]
    assert paths["skipped"].read_text(encoding="utf-8") == ""
    assert paths["canceled"].read_text(encoding="utf-8") == ""
    assert retry_manifest["retry_failed_only"] == [options.url]
    assert retry_manifest["retry_canceled_only"] == []


def test_failed_mirror_artifacts_retry_seed_without_stats(tmp_path: Path) -> None:
    options = SiteDownloadOptions(url="https://example.com/docs/", output_dir=tmp_path)
    result = DownloadResult(
        status=DownloadStatus.failed,
        url=options.url,
        message="wget2 exited 8",
        ydl_opts={"backend": "wget2"},
    )

    paths = _write_mirror_artifacts(options, result, backend="wget2")

    assert paths["failed"].read_text(encoding="utf-8").splitlines() == [options.url]
    retry_manifest = json.loads(paths["retry_manifest"].read_text(encoding="utf-8"))
    assert retry_manifest["retry_failed_only"] == [options.url]


def test_canceled_mirror_artifacts_publish_canceled_retry_seed(tmp_path: Path) -> None:
    options = SiteDownloadOptions(url="https://example.com/docs/", output_dir=tmp_path)
    result = DownloadResult(
        status=DownloadStatus.canceled,
        url=options.url,
        message="canceled by operator",
        ydl_opts={"backend": "wget2"},
    )

    paths = _write_mirror_artifacts(options, result, backend="wget2")

    assert paths["canceled"].read_text(encoding="utf-8").splitlines() == [options.url]
    retry_manifest = json.loads(paths["retry_manifest"].read_text(encoding="utf-8"))
    assert retry_manifest["retry_canceled_only"] == [options.url]
    assert retry_manifest["retry_failed_only"] == []


def test_skipped_mirror_artifacts_publish_consistent_skipped_retry_seed(
    tmp_path: Path,
) -> None:
    options = SiteDownloadOptions(url="https://example.com/docs/", output_dir=tmp_path)
    result = DownloadResult(
        status=DownloadStatus.skipped,
        url=options.url,
        message="no remote changes",
        ydl_opts={"backend": "wget2"},
    )

    paths = _write_mirror_artifacts(options, result, backend="wget2")

    summary = json.loads(paths["latest_summary"].read_text(encoding="utf-8"))
    retry_manifest = json.loads(paths["retry_manifest"].read_text(encoding="utf-8"))
    assert summary["total"] == 1
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0
    assert summary["skipped"] == 1
    assert summary["canceled"] == 0
    assert paths["skipped"].read_text(encoding="utf-8").splitlines() == [options.url]
    assert retry_manifest["retry_skipped_unknowns_only"] == [options.url]
    assert retry_manifest["skipped_urls"] == [options.url]


def test_batch_live_progress_marks_pre_backend_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/archive.zip\n", encoding="utf-8")
    events: list[ProgressEvent] = []

    class FakeReporter:
        def __init__(self, _console, **_kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def seed_entries(self, entries, *, kind=None):
            for entry in entries:
                events.append(
                    ProgressEvent(
                        engine=EngineKind.unknown,
                        status="queued",
                        phase=ProgressPhase.download,
                        line_no=entry.line_no,
                        item_id=str(entry.line_no),
                        url=entry.url,
                    )
                )

        def hook(self, event: ProgressEvent) -> None:
            events.append(event)

        def hook_for(self, **_kwargs):
            return lambda _event: None

    def fail_run(*_args, **_kwargs):
        raise AtlasError("backend missing")

    monkeypatch.setattr("atlas.cli.BatchProgressReporter", FakeReporter)
    monkeypatch.setattr("atlas.cli._try_run_aria2_batch_queue", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("atlas.optimizer.probe_direct_file", lambda url: DirectFileProbe(url=url))
    monkeypatch.setattr("atlas.cli._run_batch_hub_plan", fail_run)

    result = runner.invoke(app, ["batch", str(batch_file), "--kind", "file"])

    assert result.exit_code == 1
    assert any(event.status == "error" and event.message == "backend missing" for event in events)


def test_batch_progress_callback_adds_adaptive_scheduler_metadata() -> None:
    from atlas.cli import _adaptive_batch_runtime_scheduler, _batch_progress_callback

    url = "https://example.com/unknown.bin"
    events: list[ProgressEvent] = []

    class FakeReporter:
        def hook(self, event: ProgressEvent) -> None:
            events.append(event)

    plan = AdaptiveDownloadPlan(
        enabled=True,
        politeness=AdaptivePoliteness.normal,
        queue_concurrency=8,
        per_host_concurrency=2,
        per_file_segments=1,
        strategy="unknown sizes: conservative queue, no speculative splitting",
        work_items=[
            WorkItem(
                url=url,
                host="example.com",
                size_class=FileSizeClass.unknown,
                bucket=WorkBucket.unknown,
                selected_backend="native",
                scheduler_decision="unknown size: cautious queue until transfer reports a total",
            )
        ],
    )
    scheduler = _adaptive_batch_runtime_scheduler(plan)
    scheduler.current_concurrency = 8
    callback = _batch_progress_callback(
        FakeReporter(),
        BatchEntry(line_no=1, url=url),
        adaptive_plan=plan,
        adaptive_scheduler=scheduler,
        adaptive_items_by_url={url: plan.work_items[0]},
    )
    assert callback is not None

    callback(
        ProgressEvent(
            engine=EngineKind.native,
            status="downloading",
            phase=ProgressPhase.download,
            total_bytes=700 * 1024 * 1024,
        )
    )

    assert len(events) == 1
    event = events[0]
    assert event.line_no == 1
    assert event.work_bucket == WorkBucket.large
    assert event.size_class == FileSizeClass.large
    assert event.reclassified_from == "unknown"
    assert event.queue_concurrency == 2
    assert event.per_host_concurrency == 2
    assert event.selected_backend == "native"
    assert "reclassified unknown to large" in (event.scheduler_decision or "")


def test_batch_adaptive_updates_do_not_depend_on_reporter() -> None:
    from atlas.cli import _adaptive_batch_runtime_scheduler, _batch_progress_callback

    url = "https://example.com/unknown.bin"
    plan = AdaptiveDownloadPlan(
        enabled=True,
        politeness=AdaptivePoliteness.normal,
        queue_concurrency=8,
        per_host_concurrency=2,
        per_file_segments=1,
        strategy="unknown sizes",
        work_items=[
            WorkItem(
                url=url,
                host="example.com",
                size_class=FileSizeClass.unknown,
                bucket=WorkBucket.unknown,
            )
        ],
    )
    scheduler = _adaptive_batch_runtime_scheduler(plan)
    scheduler.current_concurrency = 8
    callback = _batch_progress_callback(
        None,
        BatchEntry(line_no=1, url=url),
        adaptive_plan=plan,
        adaptive_scheduler=scheduler,
        adaptive_items_by_url={url: plan.work_items[0]},
    )
    assert callback is not None

    callback(
        ProgressEvent(
            engine=EngineKind.native,
            status="downloading",
            phase=ProgressPhase.download,
            total_bytes=700 * 1024 * 1024,
        )
    )

    assert scheduler.current_concurrency == 2


def test_batch_site_plan_passes_process_control_to_mirror_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/docs/"
    route = EngineRoute(
        kind=HubKind.site,
        engine=EngineKind.wget2,
        reason="test",
        url=url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(route=route, output=tmp_path),
        options=SiteDownloadOptions(url=url, output_dir=tmp_path),
    )
    process_control = ProcessControl()
    captured_controls: list[ProcessControl | None] = []

    class FakeSiteMirrorAdapter:
        def run(self, options, *, progress_callback=None, control=None):
            _ = options, progress_callback
            captured_controls.append(control)
            return DownloadResult(
                status=DownloadStatus.success,
                url=url,
                message="ok",
            )

    monkeypatch.setattr("atlas.cli.SiteMirrorAdapter", FakeSiteMirrorAdapter)

    result = _run_batch_hub_plan(
        AtlasSettings(output_dir=tmp_path),
        plan,
        progress_hooks=None,
        postprocessor_hooks=None,
        progress_callback=None,
        process_control=process_control,
    )

    assert result.status == DownloadStatus.success
    assert captured_controls == [process_control]


def test_batch_file_plan_checks_active_process_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/file.zip"
    route = EngineRoute(
        kind=HubKind.file,
        engine=EngineKind.native,
        reason="test",
        url=url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(route=route, output=tmp_path / "file.zip"),
        options=FileDownloadOptions(url=url, output_dir=tmp_path),
    )
    process_control = ProcessControl()

    class FakeDirectFileAdapter:
        def run(self, options, *, progress_callback=None):
            process_control.cancel("stop active file")
            assert progress_callback is not None
            progress_callback(
                ProgressEvent(
                    engine=EngineKind.native,
                    status="downloading",
                    phase=ProgressPhase.download,
                    kind=HubKind.file,
                    url=options.url,
                    downloaded_bytes=1,
                )
            )
            return DownloadResult(
                status=DownloadStatus.success,
                url=url,
                message="should not complete",
            )

    monkeypatch.setattr("atlas.cli.DirectFileAdapter", FakeDirectFileAdapter)

    with pytest.raises(RuntimeError, match="stop active file"):
        _run_batch_hub_plan(
            AtlasSettings(output_dir=tmp_path),
            plan,
            progress_hooks=None,
            postprocessor_hooks=None,
            progress_callback=lambda _event: None,
            process_control=process_control,
        )


def test_batch_media_plan_checks_active_process_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/watch?v=1"
    route = EngineRoute(
        kind=HubKind.video,
        engine=EngineKind.ytdlp,
        reason="test",
        url=url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(route=route, output=tmp_path),
        options=VideoDownloadOptions(url=url, output_dir=tmp_path),
    )
    process_control = ProcessControl()

    class FakeEngine:
        def download_video(self, options, *, progress_hooks=None, postprocessor_hooks=None):
            _ = options, postprocessor_hooks
            process_control.cancel("stop active media")
            assert progress_hooks is not None
            for hook in progress_hooks:
                hook({"status": "downloading"})
            return DownloadResult(
                status=DownloadStatus.success,
                url=url,
                message="should not complete",
            )

    monkeypatch.setattr("atlas.cli._engine", lambda _settings: FakeEngine())
    monkeypatch.setattr("atlas.cli.ensure_download_dependencies", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="stop active media"):
        _run_batch_hub_plan(
            AtlasSettings(output_dir=tmp_path),
            plan,
            progress_hooks=None,
            postprocessor_hooks=None,
            progress_callback=None,
            process_control=process_control,
        )


def test_hub_plan_preview_omits_redundant_intent_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/file.zip"
    route = EngineRoute(
        kind=HubKind.file,
        engine=EngineKind.native,
        reason="test",
        url=url,
        output_dir=tmp_path,
    )
    session = SmartDownloadSession(
        source=url,
        detected_kind="file",
        intent="file",
        session_type="file",
        scheduler_policy={"mode": "adaptive"},
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(route=route, output=tmp_path, session=session),
        options=SiteDownloadOptions(url=url, output_dir=tmp_path),
    )
    captured_rows: list[tuple[str, str]] = []

    def fake_metadata_panel(_title: str, rows: list[tuple[str, str]]) -> None:
        captured_rows.extend(rows)

    monkeypatch.setattr("atlas.cli._metadata_panel", fake_metadata_panel)

    _print_hub_plan(plan)

    labels = [label for label, _value in captured_rows]
    assert "Session" in labels
    assert "Intent" not in labels


def test_batch_progress_reporter_is_constructed_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.batch import BatchControl, BatchOperatorController
    from atlas.cli import _batch_progress_reporter
    from atlas.progress import ProgressMode, WorkPanelContext

    monkeypatch.setattr("atlas.cli.should_use_alternate_screen", lambda *_args, **_kwargs: True)
    context = WorkPanelContext(queue_count=5, safety_badges=("adaptive normal",))
    controller = BatchOperatorController(BatchControl())

    reporter = _batch_progress_reporter(
        concurrency=3,
        progress_mode=ProgressMode.full,
        total=5,
        work_context=context,
        operator_controller=controller,
    )

    assert reporter.concurrency == 3
    assert reporter.total == 5
    assert reporter.mode == ProgressMode.full
    assert reporter.work_context == context
    assert reporter.operator_controller is controller
    assert reporter.alternate_screen is True


def test_batch_work_context_includes_adaptive_thread_and_speed_notes() -> None:
    from atlas.cli import _batch_work_context
    from atlas.models import AdaptiveDownloadPlan, AdaptivePoliteness

    context = _batch_work_context(
        queue_count=4,
        concurrency=3,
        allow_sites=False,
        allow_dirs=False,
        adaptive_plan=AdaptiveDownloadPlan(
            enabled=True,
            politeness=AdaptivePoliteness.normal,
            queue_concurrency=3,
            per_host_concurrency=2,
            per_file_segments=8,
            speed_limit="1M",
            backend="aria2",
            strategy="large files: low queue concurrency with ranged segments",
        ),
    )

    assert context.queue_count == 4
    assert "adaptive normal" in context.safety_badges
    assert "per-host 2" in context.safety_badges
    assert "segments 8" in context.safety_badges
    assert "speed 1M" in context.safety_badges


def test_batch_json_summary(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "# skip\nhttps://www.youtube.com/watch?v=1\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["batch", str(batch_file), "--dry-run", "--json"])

    assert result.exit_code == 0
    assert '"kind": "video"' in result.output
    assert '"succeeded": 1' in result.output
    assert '"skipped": 1' in result.output


def test_batch_json_uses_the_same_shared_queue_strategy_as_human_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    url = "https://example.com/archive.iso"
    batch_file.write_text(f"{url}\n", encoding="utf-8")
    calls: list[object] = []

    def fake_shared(*_args: object, **kwargs: object) -> BatchSummary:
        calls.append(kwargs.get("reporter"))
        return BatchSummary(
            kind=BatchKind.file,
            total=1,
            succeeded=1,
            results=[
                BatchItemResult(
                    entry=BatchEntry(line_no=1, url=url),
                    status=DownloadStatus.success,
                    message="Saved",
                )
            ],
        )

    def unexpected_runtime(*_args: object, **_kwargs: object) -> BatchSummary:
        raise AssertionError("JSON output changed the batch execution strategy")

    monkeypatch.setattr("atlas.cli._try_run_aria2_batch_queue", fake_shared)
    monkeypatch.setattr("atlas.cli.run_batch_concurrent", unexpected_runtime)

    result = runner.invoke(app, ["batch", str(batch_file), "--json"])

    assert result.exit_code == 0
    assert calls == [None]
    assert json.loads(result.output)["succeeded"] == 1


def test_shared_aria2_path_does_not_advertise_unbound_live_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/archive.iso\n", encoding="utf-8")
    controllers: list[object] = []

    class FakeReporter:
        def __init__(self, _console, **kwargs: object) -> None:
            self.operator_controller = kwargs.get("operator_controller")
            controllers.append(self.operator_controller)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def seed_entries(self, _entries, *, kind=None) -> None:
            return None

    monkeypatch.setattr("atlas.cli.BatchProgressReporter", FakeReporter)
    monkeypatch.setattr(
        "atlas.cli._try_run_aria2_batch_queue",
        lambda *_args, **_kwargs: BatchSummary(kind=BatchKind.file, total=1),
    )

    result = runner.invoke(app, ["batch", str(batch_file), "--kind", "file"])

    assert result.exit_code == 0
    assert controllers == [None]


def test_batch_command_passes_control_to_runtime_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlas.batch import BatchControl

    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/file.txt\n", encoding="utf-8")
    controls: list[object] = []

    def fake_run_batch_concurrent(*_args: object, **kwargs: object) -> BatchSummary:
        controls.append(kwargs.get("control"))
        return BatchSummary(kind=BatchKind.file, total=1)

    monkeypatch.setattr("atlas.cli.run_batch_concurrent", fake_run_batch_concurrent)
    monkeypatch.setattr("atlas.cli._try_run_aria2_batch_queue", lambda *_a, **_k: None)

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--kind", "file", "--json"],
    )

    assert result.exit_code == 0
    assert len(controls) == 1
    assert isinstance(controls[0], BatchControl)


def test_batch_video_codec_dry_run_json(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://www.youtube.com/watch?v=1\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "batch",
            str(batch_file),
            "--type",
            "video",
            "--video-codec",
            "hevc",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"video_codec": "hevc"' in result.output
    assert "hvc1|hev1" in result.output


def test_batch_audio_codec_dry_run_json(tmp_path: Path) -> None:
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://www.youtube.com/watch?v=1\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "batch",
            str(batch_file),
            "--type",
            "audio",
            "--codec",
            "mp3",
            "--audio-quality",
            "4",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert '"codec": "mp3"' in result.output
    assert '"audio_quality": 4' in result.output


def test_batch_auto_mixed_routing_dry_run_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "\n".join(
            [
                "https://www.youtube.com/watch?v=abc",
                "https://example.com/releases/app.dmg",
                "https://example.com/docs/",
                "https://rumble.com/v123-example.html",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["batch", str(batch_file), "--dry-run", "--json"])

    assert result.exit_code == 0
    assert '"kind": "auto"' in result.output
    assert '"succeeded": 3' in result.output
    assert '"skipped": 1' in result.output
    assert '"engine": "yt-dlp"' in result.output
    assert '"kind": "file"' in result.output
    assert '"backend": "aria2"' in result.output
    assert "Skipped possible website or directory mirror" in result.output


def test_batch_auto_routes_extensionless_direct_file_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://httpbingo.org/range/1024\n", encoding="utf-8")

    result = runner.invoke(app, ["batch", str(batch_file), "--dry-run", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["succeeded"] == 1
    assert payload["skipped"] == 0
    assert payload["results"][0]["plan"]["route"]["kind"] == "file"


def test_batch_auto_skips_explicit_playlist_without_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text(
        "https://www.youtube.com/playlist?list=PL6B3937A5D230E335\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["batch", str(batch_file), "--kind", "auto", "--dry-run", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["failed"] == 0
    assert payload["skipped"] == 1
    assert payload["results"][0]["message"].startswith("Skipped explicit playlist URL")


def test_batch_auto_allows_sites_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/docs/\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--allow-sites", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert '"succeeded": 1' in result.output
    assert '"skipped": 0' in result.output
    assert '"kind": "site"' in result.output
    assert '"backend": "wget2"' in result.output


def test_batch_auto_skips_possible_directories_without_allow_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/pub/\n", encoding="utf-8")

    result = runner.invoke(app, ["batch", str(batch_file), "--dry-run", "--json"])

    assert result.exit_code == 0
    assert '"succeeded": 0' in result.output
    assert '"skipped": 1' in result.output
    assert "Skipped possible website or directory mirror" in result.output


def test_batch_auto_allows_directories_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/pub/\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["batch", str(batch_file), "--allow-dirs", "--dry-run", "--json"],
    )

    assert result.exit_code == 0
    assert '"succeeded": 1' in result.output
    assert '"skipped": 0' in result.output
    assert '"kind": "dir"' in result.output


def test_batch_explicit_directory_kind_requires_allow_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/pub/\n", encoding="utf-8")

    blocked = runner.invoke(
        app,
        ["batch", str(batch_file), "--kind", "dir", "--dry-run", "--json"],
    )
    allowed = runner.invoke(
        app,
        ["batch", str(batch_file), "--kind", "dir", "--allow-dirs", "--dry-run", "--json"],
    )

    assert blocked.exit_code == 0
    assert '"succeeded": 0' in blocked.output
    assert '"skipped": 1' in blocked.output
    assert allowed.exit_code == 0
    assert '"succeeded": 1' in allowed.output
    assert '"kind": "dir"' in allowed.output


def test_batch_human_summary_stays_readable_at_40_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.backends.shutil.which", lambda name: f"/opt/bin/{name}")
    batch_file = tmp_path / "urls.txt"
    batch_file.write_text("https://example.com/releases/app.dmg\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["--plain", "batch", str(batch_file), "--dry-run"],
        env={"COLUMNS": "40"},
    )

    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert "file via aria2" in result.output
    assert result.output.count("file via") == 1
    assert "https://example.com/releases/app.dmg" in result.output
    assert max(len(line) for line in result.output.splitlines()) <= 40
