from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text
from rich.theme import Theme

from atlas.config import AtlasSettings
from atlas.directory_index import DirectoryEntry, DirectoryIndex
from atlas.errors import AtlasError
from atlas.media_capabilities import MediaCapabilityResolver, MediaProfile
from atlas.menu import (
    MENU_CAPABILITIES,
    SCRIPT_ONLY_COMMANDS,
    BatchSourceChoice,
    BatchUrlScanChoice,
    CompletionChoice,
    DirectoryExplorerChoice,
    FlowResult,
    MainMenuChoice,
    MenuChoice,
    PlanMenuChoice,
    PlanRecoveryChoice,
    PromptUI,
    QuestionaryPromptUI,
    ScanEmptyChoice,
    ScanFailedChoice,
    SetupGateChoice,
    _apply_customize_overlay,
    _batch_file_plan_flow,
    _completion_loop,
    _customize_choices,
    _customize_options,
    _downloadable_links_from_directory_index,
    _downloadable_links_from_scan,
    _export_failed_session_flow,
    _files_mirrors_choices,
    _launcher_header_panel,
    _main_choices,
    _mapping_diff_fields,
    _media_choices,
    _menu_footer,
    _menu_shortcut_fields,
    _normalized_directory_url,
    _open_path,
    _primary_saved_path,
    _print_batch_plan,
    _print_completion_summary,
    _print_deep_directory_scan_summary,
    _print_directory_explorer,
    _print_launcher,
    _print_media_profile_context,
    _print_menu_plan,
    _print_scan_failed,
    _print_url_scan_summary,
    _questionary_style,
    _reveal_path,
    _runtime_tool_statuses,
    _scan_directory_roots,
    _scan_looks_like_directory_index,
    _session_choices,
    _settings_choices,
    _tool_choices,
    _url_should_scan_before_auto_plan,
    _with_menu_status,
    _write_menu_batch_file,
    build_audio_options,
    build_directory_options,
    build_file_options,
    build_site_options,
    build_video_options,
    can_auto_launch_menu,
    menu_capability_command_names,
    run_interactive_menu,
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
    EngineKind,
    EngineRoute,
    FileDownloadOptions,
    FormatInfo,
    FpsChoice,
    HdrChoice,
    HttpsEnforceMode,
    HubKind,
    MediaInfo,
    MetalinkPreferredProtocol,
    OptimizedDownloadPlan,
    OrganizeMode,
    PreferFamily,
    ProgressMode,
    QualityIntent,
    ResolutionChoice,
    ScanStatus,
    SiteDownloadOptions,
    SubtitleMode,
    VerifySigMode,
    VideoCodecChoice,
    VideoDownloadOptions,
    WorkItem,
)
from atlas.optimizer import HubExecutionPlan
from atlas.passthrough import BackendTool
from atlas.theme import (
    ATLAS_PANEL_STYLE,
    ATLAS_TITLE_STYLE,
    AtlasThemeName,
    configure_visuals,
    reset_visuals,
    resolve_theme,
)
from atlas.views import SmartSessionView


@pytest.fixture(autouse=True)
def installed_runtime_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep menu tests independent of tools and config installed on the host."""

    config_file = tmp_path / "config.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("atlas.menu.config_path", lambda: config_file)
    monkeypatch.setattr("atlas.menu.shutil.which", lambda name: f"/opt/bin/{name}")


class FakeStream:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


class FakePrompts(PromptUI):
    def __init__(
        self,
        *,
        selects: list[object | None],
        multi_selects: list[list[object] | None] | None = None,
        texts: list[str | None] | None = None,
        secrets: list[str | None] | None = None,
        confirms: list[bool | None] | None = None,
    ) -> None:
        self.selects = selects
        self.multi_selects = multi_selects or []
        self.texts = texts or []
        self.secrets = secrets or []
        self.confirms = confirms or []
        self.seen_selects: list[tuple[str, list[str]]] = []
        self.seen_multi_selects: list[tuple[str, list[str]]] = []
        self.seen_confirms: list[str] = []

    def select(self, message: str, choices: Sequence[MenuChoice]) -> object | None:
        self.seen_selects.append((message, [choice.label for choice in choices]))
        return self.selects.pop(0)

    def multi_select(self, message: str, choices: Sequence[MenuChoice]) -> list[object] | None:
        self.seen_multi_selects.append((message, [choice.label for choice in choices]))
        return self.multi_selects.pop(0)

    def text(self, _message: str, *, default: str = "") -> str | None:
        _ = default
        return self.texts.pop(0)

    def secret(self, _message: str) -> str | None:
        return self.secrets.pop(0)

    def confirm(self, _message: str, *, default: bool = False) -> bool | None:
        _ = default
        self.seen_confirms.append(_message)
        return self.confirms.pop(0)


class FakeActions:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.planned: list[tuple[object, HubKind]] = []
        self.executed: list[HubExecutionPlan] = []
        self.formats_runs: list[str] = []
        self.media_infos: dict[str, MediaInfo] = {}
        self.media_probe_calls: list[tuple[str, bool]] = []
        self.batch_runs: list[
            tuple[Path, BatchKind, int | None, bool, bool, VideoCodecChoice, AudioCodec, int, bool]
        ] = []
        self.resumed_sessions: list[tuple[Path | None, bool]] = []
        self.retried_sessions: list[tuple[Path | None, bool]] = []
        self.inspected_sessions: list[Path | None] = []
        self.exported_sessions: list[tuple[Path | None, Path | None]] = []
        self.backend_runs: list[tuple[BackendTool, list[str], bool]] = []
        self.scan_items: dict[str, WorkItem] = {}
        self.scan_calls: list[str] = []
        self.config_file_opened = False
        self.setup_runs = 0
        self.setup_plan_runs = 0
        self.setup_install_runs = 0
        self.update_runs = 0

    def build_plan(self, options, kind: HubKind) -> HubExecutionPlan:
        self.planned.append((options, kind))
        engine = EngineKind.ytdlp if kind in {HubKind.video, HubKind.audio} else EngineKind.native
        route = EngineRoute(
            kind=kind,
            engine=engine,
            reason="test",
            url=options.url,
            output_dir=options.output_dir,
        )
        return HubExecutionPlan(
            route=route,
            preview=OptimizedDownloadPlan(
                route=route,
                output=options.output_dir,
                summary={"kind": kind.value},
            ),
            options=options,
        )

    def print_plan(self, _plan: HubExecutionPlan) -> None:
        return None

    def execute_plan(self, plan: HubExecutionPlan) -> list[Path]:
        self.executed.append(plan)
        if plan.options.dry_run:
            return []
        return [self.output_dir / "download.bin"]

    def run_info(self, _url: str) -> None:
        return None

    def run_formats(self, _url: str) -> None:
        self.formats_runs.append(_url)

    def probe_media(self, url: str, *, playlist: bool = False) -> MediaInfo:
        self.media_probe_calls.append((url, playlist))
        return self.media_infos.get(url, MediaInfo(title="Example", formats=[]))

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
        self.batch_runs.append(
            (
                file,
                kind,
                concurrency,
                allow_sites,
                allow_dirs,
                video_codec,
                audio_codec,
                audio_quality,
                dry_run,
            )
        )

    def resume_session(self, session: Path | None, *, dry_run: bool) -> None:
        self.resumed_sessions.append((session, dry_run))

    def retry_failed_session(self, session: Path | None, *, dry_run: bool) -> None:
        self.retried_sessions.append((session, dry_run))

    def inspect_session(self, session: Path | None) -> None:
        self.inspected_sessions.append(session)

    def export_failed_session(self, session: Path | None, *, output: Path | None) -> None:
        self.exported_sessions.append((session, output))

    def scan_url(self, url: str) -> WorkItem:
        self.scan_calls.append(url)
        return self.scan_items.get(
            url,
            WorkItem(url=url, host="example.com", discovered_links=[]),
        )

    def run_backend_tool(self, tool: BackendTool, args: list[str], *, dry_run: bool) -> None:
        self.backend_runs.append((tool, args, dry_run))

    def run_doctor(self) -> None:
        return None

    def run_setup(self) -> None:
        self.setup_runs += 1

    def show_setup_plan(self) -> None:
        self.setup_plan_runs += 1

    def run_setup_install(self) -> None:
        self.setup_install_runs += 1

    def run_update(self) -> None:
        self.update_runs += 1

    def show_config(self) -> None:
        return None

    def show_config_path(self) -> None:
        return None

    def open_config_file(self) -> None:
        self.config_file_opened = True


class FakeQuestionaryPrompt:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def ask(self) -> object:
        return self.answer


class FakeQuestionary:
    def __init__(self, *, text_answer: object = "  https://example.com/archive.zip  ") -> None:
        self.calls: list[tuple[str, str, list[str]]] = []
        self.autocomplete_kwargs: list[dict[str, object]] = []
        self.select_kwargs: list[dict[str, object]] = []
        self.checkbox_kwargs: list[dict[str, object]] = []
        self.text_kwargs: list[dict[str, object]] = []
        self.password_kwargs: list[dict[str, object]] = []
        self.confirm_kwargs: list[dict[str, object]] = []
        self.text_answer = text_answer

    def autocomplete(self, message: str, *, choices: list[str], **kwargs) -> FakeQuestionaryPrompt:
        self.calls.append(("autocomplete", message, choices))
        self.autocomplete_kwargs.append(kwargs)
        return FakeQuestionaryPrompt(choices[-1])

    def select(self, message: str, *, choices: list[str], **kwargs) -> FakeQuestionaryPrompt:
        self.calls.append(("select", message, choices))
        self.select_kwargs.append(kwargs)
        return FakeQuestionaryPrompt(choices[0])

    def checkbox(self, message: str, *, choices: list[str], **kwargs) -> FakeQuestionaryPrompt:
        self.calls.append(("checkbox", message, choices))
        self.checkbox_kwargs.append(kwargs)
        return FakeQuestionaryPrompt([choices[0], choices[-1]])

    def text(self, message: str, **kwargs) -> FakeQuestionaryPrompt:
        self.calls.append(("text", message, []))
        self.text_kwargs.append(kwargs)
        return FakeQuestionaryPrompt(self.text_answer)

    def password(self, message: str, **kwargs) -> FakeQuestionaryPrompt:
        self.calls.append(("password", message, []))
        self.password_kwargs.append(kwargs)
        return FakeQuestionaryPrompt(self.text_answer)

    def confirm(self, message: str, **kwargs) -> FakeQuestionaryPrompt:
        self.calls.append(("confirm", message, []))
        self.confirm_kwargs.append(kwargs)
        return FakeQuestionaryPrompt(True)


class InstructionRejectingQuestionary(FakeQuestionary):
    def select(self, message: str, *, choices: list[str], **kwargs) -> FakeQuestionaryPrompt:
        self.calls.append(("select", message, choices))
        self.select_kwargs.append(kwargs)
        if "instruction" in kwargs:
            msg = "PromptSession.__init__() got an unexpected keyword argument 'instruction'"
            raise TypeError(msg)
        return FakeQuestionaryPrompt(choices[0])


class FakeQuestionaryStyle:
    @classmethod
    def from_dict(cls, style_map: dict[str, str]) -> dict[str, str]:
        return style_map


class FakeStyledQuestionary:
    Style = FakeQuestionaryStyle


@pytest.fixture
def settings(tmp_path: Path) -> AtlasSettings:
    return AtlasSettings(output_dir=tmp_path / "out", archive_file=tmp_path / "archive.txt")


def _registered_command_names(app: object) -> set[str]:
    commands = {
        command.name or command.callback.__name__.replace("_", "-")
        for command in app.registered_commands
        if command.callback is not None
    }
    commands.update(group.name for group in app.registered_groups)
    return commands


def test_can_auto_launch_menu_requires_tty_and_no_automation() -> None:
    assert can_auto_launch_menu(stdin=FakeStream(True), stdout=FakeStream(True), env={})
    assert not can_auto_launch_menu(stdin=FakeStream(False), stdout=FakeStream(True), env={})
    assert not can_auto_launch_menu(stdin=FakeStream(True), stdout=FakeStream(False), env={})
    assert not can_auto_launch_menu(
        stdin=FakeStream(True),
        stdout=FakeStream(True),
        env={"CI": "1"},
    )
    assert can_auto_launch_menu(
        stdin=FakeStream(True),
        stdout=FakeStream(True),
        env={"CI": "false", "GITHUB_ACTIONS": "0"},
    )
    assert not can_auto_launch_menu(
        stdin=FakeStream(True),
        stdout=FakeStream(True),
        env={"ATLAS_NO_MENU": "1"},
    )


def test_main_menu_labels_are_compact() -> None:
    assert [choice.label for choice in _main_choices()] == [
        "Paste URL",
        "Media",
        "Files",
        "Batch",
        "Sessions",
        "Tools",
        "Settings",
        "Quit",
    ]


def test_menu_help_lists_only_controls_the_prompt_adapter_supports() -> None:
    rendered = SmartSessionView(title="atlas").render_to_text(
        SmartSessionView(title="atlas").shortcut_help_overlay(_menu_shortcut_fields()),
        width=80,
    )

    assert "Move the highlighted choice" in rendered
    assert "Filter the current choice list" in rendered
    assert "ctrl-c" in rendered
    assert "Cancel item" not in rendered
    assert "retry failed" not in rendered


def test_menu_footer_advertises_only_supported_controls() -> None:
    try:
        configure_visuals(unicode=True, color=True, plain=False, env={})

        assert _menu_footer().plain == "↑/↓ move   enter select   type to filter   ctrl-c quit"
        assert _menu_footer(multi=True).plain == (
            "↑/↓ move   space select   enter continue   type to filter   ctrl-c quit"
        )
        assert _menu_footer(back="back").plain.endswith("ctrl-c back")
    finally:
        reset_visuals()


def test_submenu_stays_open_after_a_nonterminal_action(settings: AtlasSettings) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.media,
            MainMenuChoice.info,
            "back",
            None,
            MainMenuChoice.quit,
        ],
        texts=["https://example.com/video"],
    )
    actions = FakeActions(settings.output_dir)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert [message for message, _choices in prompts.seen_selects] == [
        "Choose workflow",
        "Media",
        "Next",
        "Media",
        "Choose workflow",
    ]


def test_operator_capabilities_are_grouped_under_submenus() -> None:
    submenu_labels = {
        choice.label
        for choices in (
            _media_choices(),
            _files_mirrors_choices(),
            _session_choices(),
            _tool_choices(),
            _settings_choices(),
        )
        for choice in choices
    }
    assert "Download video" in submenu_labels
    assert "Browse directory" in submenu_labels
    assert "Export URLs" in submenu_labels
    assert "Advanced backend" in submenu_labels
    assert "Config" in submenu_labels


def test_menu_capability_registry_covers_operator_commands() -> None:
    assert [capability.id for capability in MENU_CAPABILITIES] == [
        "download_video",
        "extract_audio",
        "download_playlist",
        "download_file",
        "mirror_website",
        "mirror_directory",
        "batch_download",
        "resume_session",
        "retry_failed",
        "inspect_session",
        "export_failed",
        "show_info",
        "show_formats",
        "advanced_backend",
        "doctor",
        "setup",
        "update",
        "config",
        "help",
    ]


def test_launcher_header_is_polished_without_redundant_paths(settings: AtlasSettings) -> None:
    try:
        configure_visuals(unicode=True, color=True, plain=False, env={})
        output = StringIO()
        prompts = FakePrompts(selects=[MainMenuChoice.quit])
        actions = FakeActions(settings.output_dir)

        run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=output))

        rendered = output.getvalue()
        assert "Smart downloads for media, files, mirrors, and batches" in rendered
        assert "Safer defaults" not in rendered
        assert prompts.seen_selects[0][0] == "Choose workflow"
        assert "What would you like to do?" not in rendered
        assert "atlas smart downloads" not in rendered
        assert "Output" not in rendered
        assert "Archive" not in rendered
        assert "↑/↓" in rendered
        assert "enter" in rendered
        assert "type to filter" in rendered
        assert "ctrl-c quit" in rendered
    finally:
        reset_visuals()


def test_launcher_header_panel_uses_semantic_title_style(settings: AtlasSettings) -> None:
    panel = _launcher_header_panel(settings)

    assert isinstance(panel.title, Text)
    assert panel.title.style == ATLAS_TITLE_STYLE
    assert panel.border_style == ATLAS_PANEL_STYLE


def test_launcher_does_not_render_preview_panel(settings: AtlasSettings) -> None:
    output = StringIO()

    _print_launcher(Console(file=output, width=120), settings)

    rendered = output.getvalue()
    assert "Smart downloads" in rendered
    assert "media" in rendered
    assert "mirrors" in rendered
    assert "Preview" not in rendered
    assert "Paste any URL and Atlas will" not in rendered
    assert "Best for:" not in rendered


def test_plain_launcher_snapshot_uses_ascii_navigation(settings: AtlasSettings) -> None:
    try:
        configure_visuals(plain=True, env={})
        output = StringIO()

        _print_launcher(Console(file=output, width=100), settings)

        rendered = output.getvalue()
        assert "up/down move" in rendered
        assert "↑" not in rendered
        assert "·" not in rendered
    finally:
        reset_visuals()


def test_every_normal_command_is_reachable_from_menu() -> None:
    from atlas.cli import app

    command_names = _registered_command_names(app)
    uncovered = command_names - SCRIPT_ONLY_COMMANDS - menu_capability_command_names()

    assert uncovered == set()
    assert {"ytdlp", "aria2", "wget2", "wget"} <= menu_capability_command_names()
    assert menu_capability_command_names(include_advanced=False).isdisjoint(
        {"ytdlp", "aria2", "wget2", "wget"}
    )


def test_download_capabilities_declare_typed_option_models() -> None:
    typed = {
        capability.id: capability.typed_options_model
        for capability in MENU_CAPABILITIES
        if capability.id
        in {
            "download_video",
            "extract_audio",
            "download_file",
            "mirror_website",
            "mirror_directory",
        }
    }

    assert typed == {
        "download_video": VideoDownloadOptions,
        "extract_audio": AudioDownloadOptions,
        "download_file": FileDownloadOptions,
        "mirror_website": SiteDownloadOptions,
        "mirror_directory": DirectoryMirrorOptions,
    }


def test_questionary_prompt_uses_visible_select_for_large_menus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        configure_visuals(unicode=True, color=True, plain=False, env={})
        fake_questionary = FakeQuestionary()
        monkeypatch.setattr(
            "atlas.menu.importlib.import_module",
            lambda _name: fake_questionary,
        )
        prompts = QuestionaryPromptUI()
        choices = [MenuChoice(f"Choice {index}", index) for index in range(9)]

        selected = prompts.select("Choose", choices)

        assert selected == 0
        assert fake_questionary.calls == [
            ("select", "Choose", [choice.label for choice in choices])
        ]
        assert fake_questionary.select_kwargs == [
            {
                "qmark": "",
                "pointer": "\u203a",
                "style": None,
                "use_arrow_keys": True,
                "use_jk_keys": False,
                "use_search_filter": True,
                "match_middle": True,
                "ignore_case": True,
                "show_selected": False,
                "instruction": " ",
            }
        ]
    finally:
        reset_visuals()


def test_questionary_prompt_retries_when_select_rejects_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        configure_visuals(unicode=True, color=True, plain=False, env={})
        fake_questionary = InstructionRejectingQuestionary()
        monkeypatch.setattr(
            "atlas.menu.importlib.import_module",
            lambda _name: fake_questionary,
        )
        prompts = QuestionaryPromptUI()
        choices = [MenuChoice(f"Choice {index}", index) for index in range(9)]

        selected = prompts.select("Choose", choices)

        assert selected == 0
        assert fake_questionary.calls == [
            ("select", "Choose", [choice.label for choice in choices]),
            ("select", "Choose", [choice.label for choice in choices]),
        ]
        assert fake_questionary.select_kwargs == [
            {
                "qmark": "",
                "pointer": "\u203a",
                "style": None,
                "use_arrow_keys": True,
                "use_jk_keys": False,
                "use_search_filter": True,
                "match_middle": True,
                "ignore_case": True,
                "show_selected": False,
                "instruction": " ",
            },
            {
                "qmark": "",
                "pointer": "\u203a",
                "style": None,
                "use_arrow_keys": True,
                "use_jk_keys": False,
                "use_search_filter": True,
                "match_middle": True,
                "ignore_case": True,
                "show_selected": False,
            },
        ]
    finally:
        reset_visuals()


def test_questionary_style_uses_theme_palette_and_plain_fallback() -> None:
    try:
        configure_visuals(theme=AtlasThemeName.dark, color=True, env={})
        dark_style = _questionary_style(FakeStyledQuestionary())
        assert dark_style is not None
        assert dark_style["highlighted"] == "fg:#000000 bg:#00d7ff bold"
        assert dark_style["pointer"] == "fg:#00d7ff bold"

        configure_visuals(theme=AtlasThemeName.light, color=True, env={})
        light_style = _questionary_style(FakeStyledQuestionary())
        assert light_style is not None
        assert light_style["highlighted"] == "fg:#ffffff bg:#005fbd bold"
        assert light_style["pointer"] == "fg:#005fbd bold"

        configure_visuals(theme=AtlasThemeName.high_contrast, color=True, env={})
        high_contrast_style = _questionary_style(FakeStyledQuestionary())
        assert high_contrast_style is not None
        assert high_contrast_style["highlighted"] == "fg:#000000 bg:#ffff00 bold"
        assert high_contrast_style["pointer"] == "fg:#00ffff bold"

        configure_visuals(color=False, env={})
        plain_style = _questionary_style(FakeStyledQuestionary())
        assert plain_style is not None
        assert plain_style["highlighted"] == "reverse bold"
        assert plain_style["pointer"] == "bold"
    finally:
        reset_visuals()


def test_questionary_prompt_uses_select_for_short_menus(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_questionary = FakeQuestionary()
    monkeypatch.setattr(
        "atlas.menu.importlib.import_module",
        lambda _name: fake_questionary,
    )
    prompts = QuestionaryPromptUI()
    choices = [MenuChoice("Start", "start"), MenuChoice("Quit", "quit")]

    selected = prompts.select("Next", choices)

    assert selected == "start"
    assert fake_questionary.calls == [("select", "Next", ["Start", "Quit"])]


def test_questionary_prompt_uses_checkbox_for_multi_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        configure_visuals(unicode=True, color=True, plain=False, env={})
        fake_questionary = FakeQuestionary()
        monkeypatch.setattr(
            "atlas.menu.importlib.import_module",
            lambda _name: fake_questionary,
        )
        prompts = QuestionaryPromptUI()
        choices = [MenuChoice("One", 1), MenuChoice("Two", 2), MenuChoice("Three", 3)]

        selected = prompts.multi_select("Choose many", choices)

        assert selected == [1, 3]
        assert fake_questionary.calls == [("checkbox", "Choose many", ["One", "Two", "Three"])]
        assert fake_questionary.checkbox_kwargs == [
            {
                "qmark": "",
                "pointer": "\u203a",
                "style": None,
                "use_search_filter": True,
                "use_jk_keys": False,
                "match_middle": True,
                "ignore_case": True,
                "instruction": " ",
            }
        ]
    finally:
        reset_visuals()


def test_questionary_prompt_uses_ascii_pointer_in_plain_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        configure_visuals(plain=True, env={})
        fake_questionary = FakeQuestionary()
        monkeypatch.setattr(
            "atlas.menu.importlib.import_module",
            lambda _name: fake_questionary,
        )
        prompts = QuestionaryPromptUI()

        selected = prompts.select("Next", [MenuChoice("Start", "start")])

        assert selected == "start"
        assert fake_questionary.select_kwargs[0]["pointer"] == ">"
    finally:
        reset_visuals()


def test_questionary_prompt_styles_text_input(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_questionary = FakeQuestionary()
    monkeypatch.setattr(
        "atlas.menu.importlib.import_module",
        lambda _name: fake_questionary,
    )
    prompts = QuestionaryPromptUI()

    answer = prompts.text("URL", default="https://example.com/")

    assert answer == "https://example.com/archive.zip"
    assert fake_questionary.calls == [("text", "URL", [])]
    assert fake_questionary.text_kwargs == [
        {
            "default": "https://example.com/",
            "qmark": "",
            "style": None,
            "instruction": " ",
        }
    ]


def test_questionary_text_keeps_blank_distinct_from_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blank_questionary = FakeQuestionary(text_answer="   ")
    monkeypatch.setattr(
        "atlas.menu.importlib.import_module",
        lambda _name: blank_questionary,
    )

    assert QuestionaryPromptUI().text("Optional value") == ""

    canceled_questionary = FakeQuestionary(text_answer=None)
    monkeypatch.setattr(
        "atlas.menu.importlib.import_module",
        lambda _name: canceled_questionary,
    )

    assert QuestionaryPromptUI().text("Optional value") is None


def test_questionary_secret_uses_hidden_password_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_questionary = FakeQuestionary(text_answer=" secret with spaces ")
    monkeypatch.setattr(
        "atlas.menu.importlib.import_module",
        lambda _name: fake_questionary,
    )

    answer = QuestionaryPromptUI().secret("HTTP password")

    assert answer == " secret with spaces "
    assert fake_questionary.calls == [("password", "HTTP password", [])]
    assert fake_questionary.password_kwargs == [{"qmark": "", "style": None, "instruction": " "}]


def test_option_diff_never_renders_secret_values() -> None:
    fields = _mapping_diff_fields(
        {"http_password": None, "headers": []},
        {
            "http_password": "supersecret",
            "headers": ["Authorization: bearer-secret"],
        },
    )

    rendered = " ".join(f"{field.label} {field.value}" for field in fields)
    assert "supersecret" not in rendered
    assert "bearer-secret" not in rendered
    assert "Http Password unset -> set" in rendered
    assert "Headers unset -> set" in rendered


def test_export_failed_session_blank_output_uses_stdout(tmp_path: Path) -> None:
    actions = FakeActions(tmp_path)
    prompts = FakePrompts(
        selects=[PlanMenuChoice.start, "back"],
        texts=["latest", ""],
    )

    result = _export_failed_session_flow(actions, prompts)

    assert result == FlowResult.back
    assert actions.exported_sessions == [(None, None)]


def test_questionary_prompt_styles_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_questionary = FakeQuestionary()
    monkeypatch.setattr(
        "atlas.menu.importlib.import_module",
        lambda _name: fake_questionary,
    )
    prompts = QuestionaryPromptUI()

    answer = prompts.confirm("Start?", default=True)

    assert answer is True
    assert fake_questionary.calls == [("confirm", "Start?", [])]
    assert fake_questionary.confirm_kwargs == [
        {
            "default": True,
            "qmark": "",
            "style": None,
            "instruction": " ",
        }
    ]


def test_menu_status_skips_spinner_when_motion_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )
        output = StringIO()
        console = Console(file=output, force_terminal=True)
        status_calls: list[str] = []

        def fake_status(*_args: object, **_kwargs: object) -> object:
            status_calls.append("called")
            raise AssertionError("status spinner should not start when motion is disabled")

        monkeypatch.setattr(console, "status", fake_status)

        result = _with_menu_status(console, "Planning", lambda: "done")

        assert result == "done"
        assert status_calls == []
    finally:
        reset_visuals()


def test_menu_shortcuts_overlay_returns_to_launcher(settings: AtlasSettings) -> None:
    output = StringIO()
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.shortcuts,
            "back",
            MainMenuChoice.quit,
        ],
    )
    actions = FakeActions(settings.output_dir)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=output))

    rendered = output.getvalue()
    assert "Shortcuts" in rendered
    assert "Move the highlighted choice" in rendered
    assert "Cancel the current prompt and go back" in rendered


def test_menu_setup_and_update_are_reachable(settings: AtlasSettings) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.setup,
            "back",
            MainMenuChoice.update,
            "back",
            MainMenuChoice.quit,
        ],
    )
    actions = FakeActions(settings.output_dir)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert actions.setup_runs == 1
    assert actions.update_runs == 1


def test_setup_gate_can_run_install_action_when_terminal(
    settings: AtlasSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.menu.config_path", lambda: Path("/missing/atlas/config.toml"))
    monkeypatch.setattr("atlas.menu.shutil.which", lambda _name: None)
    prompts = FakePrompts(
        selects=[
            "install",
            MainMenuChoice.quit,
        ],
    )
    actions = FakeActions(settings.output_dir)

    run_interactive_menu(
        settings,
        actions,
        prompts=prompts,
        console=Console(file=StringIO(), force_terminal=True),
    )

    assert actions.setup_install_runs == 1


def test_setup_gate_plan_is_read_only_and_keeps_the_gate_open(
    settings: AtlasSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.menu.config_path", lambda: Path("/missing/atlas/config.toml"))
    monkeypatch.setattr("atlas.menu.shutil.which", lambda _name: None)
    prompts = FakePrompts(
        selects=[
            SetupGateChoice.plan,
            "back",
            SetupGateChoice.limited,
            MainMenuChoice.quit,
        ],
    )
    actions = FakeActions(settings.output_dir)

    run_interactive_menu(
        settings,
        actions,
        prompts=prompts,
        console=Console(file=StringIO(), force_terminal=True),
    )

    assert actions.setup_plan_runs == 1
    assert actions.setup_runs == 0


def test_runtime_tool_status_uses_full_runtime_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    present = {"ffmpeg", "ffprobe"}
    monkeypatch.setattr(
        "atlas.menu.shutil.which",
        lambda name: f"/opt/homebrew/bin/{name}" if name in present else None,
    )

    statuses = _runtime_tool_statuses()

    assert [status.tool.executable for status in statuses] == [
        "ffmpeg",
        "ffprobe",
        "aria2c",
        "wget2",
        "wget",
    ]
    assert [status.installed for status in statuses] == [True, True, False, False, False]


def test_menu_config_flow_can_open_config_file(settings: AtlasSettings) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.config,
            "open",
            "back",
            MainMenuChoice.quit,
        ],
    )
    actions = FakeActions(settings.output_dir)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert actions.config_file_opened is True


def test_menu_video_dry_run_maps_to_typed_request(settings: AtlasSettings, tmp_path: Path) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.video,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["https://www.youtube.com/watch?v=abc"],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    first_options, first_kind = actions.planned[0]
    assert isinstance(first_options, VideoDownloadOptions)
    assert first_options.url == "https://www.youtube.com/watch?v=abc"
    assert first_options.playlist is False
    assert first_kind == HubKind.video
    assert actions.media_probe_calls == [("https://www.youtube.com/watch?v=abc", False)]
    assert actions.executed[0].options.dry_run is True


def test_menu_video_uses_probe_profile_before_planning(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    url = "https://www.youtube.com/watch?v=abc"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.video,
            MediaProfile.compatible,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[url],
    )
    actions = FakeActions(tmp_path)
    actions.media_infos[url] = MediaInfo(
        title="Example",
        extractor="YouTube",
        formats=[
            FormatInfo(
                format_id="137",
                ext="mp4",
                resolution="1920x1080",
                vcodec="avc1.640028",
                acodec="none",
                filesize=190_000_000,
            ),
            FormatInfo(
                format_id="140",
                ext="m4a",
                resolution="audio only",
                vcodec="none",
                acodec="mp4a.40.2",
                filesize=14_000_000,
            ),
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    first_options, first_kind = actions.planned[0]
    assert first_kind == HubKind.video
    assert isinstance(first_options, VideoDownloadOptions)
    assert first_options.format == "137+140"
    assert first_options.container == Container.mp4
    assert first_options.quality == QualityIntent.compatible
    assert actions.executed[0].options.dry_run is True
    profile_prompt = next(
        labels for message, labels in prompts.seen_selects if message == "Choose profile"
    )
    assert any("Apple compatible" in label and "MP4" in label for label in profile_prompt)
    assert all("no conversion" not in label for label in profile_prompt)


def test_media_profile_context_uses_semantic_styles_without_markup() -> None:
    try:
        configure_visuals(color=False, unicode=True, env={})
        media = MediaInfo(
            title="Example [Interview]",
            extractor="YouTube",
            duration=372,
            formats=[
                FormatInfo(format_id="137", ext="mp4", resolution="1080p", vcodec="avc1"),
                FormatInfo(format_id="140", ext="m4a", acodec="mp4a", filesize=12_000_000),
            ],
        )
        catalog = MediaCapabilityResolver.from_info(media).catalog
        output = StringIO()

        _print_media_profile_context(Console(file=output, force_terminal=True), media, catalog)

        rendered = output.getvalue()
        assert "Smart downloads for media, files, mirrors, and batches" not in rendered
        assert "Media \u203a Download video" in rendered
        assert "Detected" in rendered
        assert "Choose profile" in rendered
        assert "Example [Interview]" in rendered
        assert "YouTube · 6:12" in rendered
        assert "1 video format · 1 audio format available" in rendered
        assert "[bold]" not in rendered
        assert "[dim]" not in rendered
        assert "\x1b[" not in rendered
    finally:
        reset_visuals()


def test_video_profile_context_uses_source_card_above_choices() -> None:
    try:
        configure_visuals(color=False, unicode=True, env={})
        media = MediaInfo(
            title="How to Get a New Identity and Disappear.",
            uploader="Into the Shadows",
            extractor="YouTube",
            duration=785,
            formats=[
                FormatInfo(format_id="137", ext="mp4", resolution="1080p", vcodec="avc1"),
                FormatInfo(format_id="140", ext="m4a", acodec="mp4a", filesize=12_000_000),
            ],
        )
        catalog = MediaCapabilityResolver.from_info(media).catalog
        output = StringIO()

        _print_media_profile_context(
            Console(file=output, force_terminal=True, color_system=None, width=100),
            media,
            catalog,
            url="https://www.youtube.com/watch?v=AICiFEiKM8c",
            kind=HubKind.video,
        )

        rendered = output.getvalue()
        assert "\u256d\u2500 Media \u203a Download video" in rendered
        assert "Source" in rendered
        assert "https://www.youtube.com/watch?v=AICiFEiKM8c" in rendered
        assert "Detected" in rendered
        assert "How to Get a New Identity and Disappear." in rendered
        assert "Into the Shadows · YouTube · 13:05" in rendered
        assert "Choose profile" in rendered
    finally:
        reset_visuals()


def test_menu_audio_profile_can_select_best_source(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    url = "https://www.youtube.com/watch?v=abc"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.audio,
            MediaProfile.audio_best,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[url],
    )
    actions = FakeActions(tmp_path)
    actions.media_infos[url] = MediaInfo(
        title="Example",
        extractor="YouTube",
        formats=[
            FormatInfo(
                format_id="251",
                ext="webm",
                resolution="audio only",
                vcodec="none",
                acodec="opus",
                filesize=12_000_000,
            ),
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    first_options, first_kind = actions.planned[0]
    assert first_kind == HubKind.audio
    assert isinstance(first_options, AudioDownloadOptions)
    assert first_options.format == "251"
    assert first_options.codec == AudioCodec.best


def test_menu_audio_mp3_profile_confirms_ffmpeg_conversion(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    url = "https://www.youtube.com/watch?v=abc"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.audio,
            MediaProfile.audio_mp3,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[url],
        confirms=[True],
    )
    actions = FakeActions(tmp_path)
    actions.media_infos[url] = MediaInfo(
        title="Example",
        extractor="YouTube",
        formats=[
            FormatInfo(
                format_id="251",
                ext="webm",
                resolution="audio only",
                vcodec="none",
                acodec="opus",
                filesize=12_000_000,
            ),
        ],
    )
    output = StringIO()

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=output))

    first_options, first_kind = actions.planned[0]
    assert first_kind == HubKind.audio
    assert isinstance(first_options, AudioDownloadOptions)
    assert first_options.format == "251"
    assert first_options.codec == AudioCodec.mp3
    assert prompts.seen_confirms == ["Continue with this profile?"]


def test_menu_blank_file_url_reprompts_without_crashing(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    valid_url = "https://example.com/archive.zip"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.file,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["   ", valid_url],
    )
    actions = FakeActions(tmp_path)
    output = StringIO()

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=output))

    assert "URL cannot be blank" in output.getvalue()
    assert actions.planned
    assert actions.planned[0][0].url == valid_url


def test_menu_smart_download_uses_auto_hub_route(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.smart,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["https://example.com/archive.zip"],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.planned[0][1] == HubKind.auto
    assert actions.planned[1][1] == HubKind.auto
    assert actions.executed[0].options.dry_run is True


def test_menu_smart_download_scans_html_indexes_before_planning(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "http://textfiles.com/directory.html"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.smart,
            BatchUrlScanChoice.recursive,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="textfiles.com",
        scan_type="HTML page",
        discovered_links=[
            "http://textfiles.com/bbs/old-bbs-list.txt",
            "http://textfiles.com/etext/index.html",
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    planned_options, planned_kind = actions.planned[0]
    assert planned_kind == HubKind.dir
    assert isinstance(planned_options, DirectoryMirrorOptions)
    assert planned_options.url == seed_url
    assert planned_options.adaptive is True
    assert actions.executed[0].options.dry_run is True
    assert any(message == "Actions" for message, _labels in prompts.seen_selects)


def test_smart_download_scan_trigger_is_limited_to_page_like_urls() -> None:
    assert _url_should_scan_before_auto_plan("http://textfiles.com/directory.html")
    assert _url_should_scan_before_auto_plan("https://example.com/files/")
    assert _url_should_scan_before_auto_plan("https://example.com/index")
    assert not _url_should_scan_before_auto_plan("https://example.com/archive.zip")
    assert not _url_should_scan_before_auto_plan("ftp://example.com/directory.html")


def test_file_only_autoindex_opens_directory_explorer() -> None:
    index = DirectoryIndex(
        source_url="https://example.com/video/",
        host="example.com",
        entries=(
            DirectoryEntry(
                name="clip.mp4",
                url="https://example.com/video/clip.mp4",
                kind="file",
            ),
        ),
        parser_name="autoindex-html",
    )
    scan = WorkItem(
        url=index.source_url,
        scan_type="directory-style HTML index",
    )

    assert _scan_looks_like_directory_index(scan, index) is True


def test_downloadable_links_from_directory_scan_excludes_page_chrome() -> None:
    scan = WorkItem(
        url="https://perso.eleaar.fr/serveur/livres/",
        host="perso.eleaar.fr",
        scan_type="directory-style HTML index",
        discovered_links=[
            "https://perso.eleaar.fr/icons/blank.gif",
            "https://perso.eleaar.fr/icons/folder.gif",
            "https://perso.eleaar.fr/serveur/livres/book.epub",
        ],
        discovered_work_items=[
            WorkItem(
                url="https://perso.eleaar.fr/serveur/livres/book.epub",
                host="perso.eleaar.fr",
                kind=HubKind.file,
                same_host=True,
            ),
            WorkItem(
                url="https://perso.eleaar.fr/serveur/livres/Folder/",
                host="perso.eleaar.fr",
                kind=HubKind.dir,
                same_host=True,
            ),
        ],
    )

    assert _downloadable_links_from_scan(scan) == [
        "https://perso.eleaar.fr/serveur/livres/book.epub"
    ]


def test_menu_directory_explorer_can_download_visible_files(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "https://example.com/serveur/"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.smart,
            DirectoryExplorerChoice.visible_files,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        multi_selects=[
            [
                DirectoryEntry(
                    name="readme.txt",
                    url="https://example.com/serveur/readme.txt",
                    kind="file",
                    visible_size=1024,
                )
            ]
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(
                url="https://example.com/serveur/cours/",
                host="example.com",
                kind=HubKind.dir,
            ),
            WorkItem(
                url="https://example.com/serveur/readme.txt",
                host="example.com",
                kind=HubKind.file,
                content_length=1024,
                file_extension=".txt",
            ),
            WorkItem(
                url="https://example.com/serveur/index.html",
                host="example.com",
                kind=HubKind.site,
                file_extension=".html",
            ),
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert actions.planned == []
    batch_file, kind, _concurrency, allow_sites, allow_dirs, *_rest = actions.batch_runs[0]
    assert kind == BatchKind.file
    assert allow_sites is False
    assert allow_dirs is False
    assert batch_file.read_text(encoding="utf-8").splitlines() == [
        "https://example.com/serveur/readme.txt"
    ]
    assert prompts.seen_multi_selects[0][0] == "Visible files"
    action_prompt = next(
        labels
        for message, labels in prompts.seen_selects
        if message == "Directory actions" and "Open a folder" in labels
    )
    assert action_prompt[:4] == [
        "Open a folder",
        "Choose files at this level",
        "Scan this folder and children (depth 2)",
        "Scan selected folders",
    ]


def test_menu_directory_explorer_deep_scans_selected_folders(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "https://example.com/serveur/"
    cours_url = "https://example.com/serveur/cours/"
    images_url = "https://example.com/serveur/images/"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.smart,
            DirectoryExplorerChoice.folders,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        multi_selects=[
            [
                DirectoryEntry(name="cours/", url=cours_url, kind="directory"),
                DirectoryEntry(name="images/", url=images_url, kind="directory"),
            ]
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(url=cours_url, host="example.com", kind=HubKind.dir),
            WorkItem(url=images_url, host="example.com", kind=HubKind.dir),
        ],
    )
    actions.scan_items[cours_url] = WorkItem(
        url=cours_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(
                url="https://example.com/serveur/cours/math.pdf",
                host="example.com",
                kind=HubKind.file,
                file_extension=".pdf",
            )
        ],
    )
    actions.scan_items[images_url] = WorkItem(
        url=images_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(
                url="https://example.com/serveur/images/photo.jpg",
                host="example.com",
                kind=HubKind.file,
                file_extension=".jpg",
            )
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert actions.scan_calls == [seed_url, cours_url, images_url]
    batch_file, kind, _concurrency, _allow_sites, allow_dirs, *_rest = actions.batch_runs[0]
    assert kind == BatchKind.file
    assert allow_dirs is False
    assert batch_file.read_text(encoding="utf-8").splitlines() == [
        "https://example.com/serveur/cours/math.pdf",
        "https://example.com/serveur/images/photo.jpg",
    ]
    assert prompts.seen_multi_selects[0][0] == "Folders"


def test_deep_directory_summary_labels_partial_discovery_honestly() -> None:
    root = "https://example.com/root/"
    partial_scan = WorkItem(
        url=root,
        final_url=root,
        host="example.com",
        kind=HubKind.dir,
        scan_status=ScanStatus.partial,
        scan_counts={"files": 1, "same_host": 1, "complete": 0},
        scan_warnings=["link extraction stopped at the safety limit"],
        discovered_work_items=[
            WorkItem(url=f"{root}visible.pdf", host="example.com", kind=HubKind.file)
        ],
    )
    output = StringIO()

    _print_deep_directory_scan_summary(
        Console(file=output),
        partial_scan,
        [root],
        [partial_scan],
    )

    rendered = output.getvalue()
    assert "Scan partial" in rendered
    assert "Scan complete" not in rendered
    assert "discovered files only" in rendered
    assert "totals are lower bounds" in rendered
    assert "link extraction stopped at the safety limit" in rendered


def test_menu_directory_everything_keeps_root_files_and_scans_nested_folders(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "https://example.com/public/"
    nested_url = "https://example.com/public/nested/"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.smart,
            DirectoryExplorerChoice.everything,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        final_url=seed_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(
                url=f"{seed_url}README.md",
                host="example.com",
                kind=HubKind.file,
                file_extension=".md",
            ),
            WorkItem(url=nested_url, host="example.com", kind=HubKind.dir),
        ],
    )
    actions.scan_items[nested_url] = WorkItem(
        url=nested_url,
        final_url=nested_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(
                url=f"{nested_url}manual.pdf",
                host="example.com",
                kind=HubKind.file,
                file_extension=".pdf",
            )
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert actions.scan_calls == [seed_url, nested_url]
    batch_file, kind, _concurrency, _allow_sites, allow_dirs, *_rest = actions.batch_runs[0]
    assert kind == BatchKind.file
    assert allow_dirs is False
    assert batch_file.read_text(encoding="utf-8").splitlines() == [
        f"{seed_url}README.md",
        f"{nested_url}manual.pdf",
    ]


def test_menu_directory_can_open_folder_then_navigate_up(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "https://example.com/public/"
    child_url = f"{seed_url}Books/"
    folder = DirectoryEntry(name="Books/", url=child_url, kind="directory")
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.smart,
            DirectoryExplorerChoice.open_folder,
            folder,
            DirectoryExplorerChoice.back,
            DirectoryExplorerChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        final_url=seed_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(url=child_url, host="example.com", kind=HubKind.dir),
        ],
    )
    actions.scan_items[child_url] = WorkItem(
        url=child_url,
        final_url=child_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(
                url=f"{child_url}manual",
                host="example.com",
                kind=HubKind.file,
            )
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert actions.scan_calls == [seed_url, child_url]
    directory_menus = [
        labels for message, labels in prompts.seen_selects if message == "Directory actions"
    ]
    assert "Up to parent folder" in directory_menus[1]
    assert "Back" in directory_menus[-1]


def test_directory_visible_queue_enforces_origin_and_keeps_extensionless_files() -> None:
    directory_index = DirectoryIndex(
        source_url="https://example.com:443/public/",
        host="example.com",
        entries=(
            DirectoryEntry(
                name="README",
                url="https://example.com/public/README",
                kind="file",
            ),
            DirectoryEntry(
                name="other-port.zip",
                url="https://example.com:444/other-port.zip",
                kind="file",
            ),
            DirectoryEntry(
                name="downgrade.zip",
                url="http://example.com/downgrade.zip",
                kind="file",
            ),
            DirectoryEntry(
                name="offsite.zip",
                url="https://evil.example/offsite.zip",
                kind="file",
            ),
            DirectoryEntry(
                name="sibling.zip",
                url="https://example.com/sibling/sibling.zip",
                kind="file",
            ),
            DirectoryEntry(
                name="offsite-folder/",
                url="https://evil.example/offsite-folder/",
                kind="directory",
            ),
        ),
    )

    assert _downloadable_links_from_directory_index(directory_index) == [
        "https://example.com/public/README"
    ]
    output = StringIO()
    _print_directory_explorer(
        Console(file=output),
        WorkItem(url=directory_index.source_url, scan_type="open directory index"),
        directory_index,
    )
    assert "offsite" not in output.getvalue()


def test_directory_scan_keeps_case_sensitive_children_and_rejects_unsafe_folders(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    root = "https://example.com/root/"
    safe_urls = [f"{root}Folder/", f"{root}folder/"]
    seed_scan = WorkItem(
        url=root,
        final_url=root,
        host="example.com",
        kind=HubKind.dir,
        discovered_work_items=[
            *(WorkItem(url=url, host="example.com", kind=HubKind.dir) for url in safe_urls),
            WorkItem(
                url="https://example.com/",
                host="example.com",
                kind=HubKind.dir,
                error="parent directory link skipped by no-parent policy",
            ),
            WorkItem(
                url="https://evil.example/root/offsite/",
                host="evil.example",
                kind=HubKind.dir,
                same_host=False,
                external_host=True,
            ),
            WorkItem(
                url="https://example.com:444/root/other-port/",
                host="example.com",
                kind=HubKind.dir,
            ),
        ],
    )
    actions = FakeActions(tmp_path)

    scans, limited = _scan_directory_roots(
        settings.model_copy(update={"dir_depth": 1}),
        actions,
        Console(file=StringIO()),
        seed_scan,
        [root],
    )

    assert limited is False
    assert actions.scan_calls == safe_urls
    assert [scan.url for scan in scans] == [root, *safe_urls]
    assert _normalized_directory_url(safe_urls[0]) != _normalized_directory_url(safe_urls[1])
    assert _normalized_directory_url("HTTPS://Example.COM:443/Folder/#fragment") == (
        "https://example.com/Folder"
    )
    assert _normalized_directory_url("https://example.com:bad/root/").endswith("/root")


def test_directory_scan_rejects_child_scan_that_redirects_outside_scope(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    root = "https://example.com/root/"
    child = f"{root}redirect/"
    escaped = "https://example.com:444/elsewhere/"
    seed_scan = WorkItem(
        url=root,
        final_url=root,
        host="example.com",
        kind=HubKind.dir,
        discovered_work_items=[
            WorkItem(url=child, host="example.com", kind=HubKind.dir),
        ],
    )
    escaped_scan = WorkItem(
        url=child,
        final_url=escaped,
        host="example.com",
        final_host="example.com",
        kind=HubKind.dir,
        discovered_work_items=[
            WorkItem(
                url=f"{escaped}payload.bin",
                host="example.com",
                kind=HubKind.file,
            )
        ],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[child] = escaped_scan
    output = StringIO()

    scans, limited = _scan_directory_roots(
        settings.model_copy(update={"dir_depth": 1}),
        actions,
        Console(file=output),
        seed_scan,
        [root],
    )

    assert limited is False
    assert scans == [seed_scan]
    assert actions.scan_calls == [child]
    assert "redirected outside" in output.getvalue()
    assert _downloadable_links_from_scan(escaped_scan) == []

    encoded_escape = escaped_scan.model_copy(
        update={
            "final_url": f"{root}%252e%252e/out/",
            "discovered_work_items": [],
        }
    )
    encoded_scans, _limited = _scan_directory_roots(
        settings.model_copy(update={"dir_depth": 0}),
        FakeActions(tmp_path),
        Console(file=StringIO()),
        encoded_escape,
        [child],
    )
    assert encoded_scans == []


def test_directory_open_folder_stays_put_after_cross_origin_redirect(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    root = "https://example.com/root/"
    child = f"{root}redirect/"
    folder = DirectoryEntry(name="redirect/", url=child, kind="directory")
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.smart,
            DirectoryExplorerChoice.open_folder,
            folder,
            "back",
            DirectoryExplorerChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[root],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[root] = WorkItem(
        url=root,
        final_url=root,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(url=child, host="example.com", kind=HubKind.dir),
        ],
    )
    actions.scan_items[child] = WorkItem(
        url=child,
        final_url="https://evil.example/elsewhere/",
        host="example.com",
        final_host="evil.example",
        scan_type="directory-style HTML index",
    )
    output = StringIO()

    run_interactive_menu(
        settings,
        actions,
        prompts=prompts,
        console=Console(file=output),
    )

    assert actions.scan_calls == [root, child]
    assert any(message == "Back to directory" for message, _labels in prompts.seen_selects)
    directory_menus = [
        labels for message, labels in prompts.seen_selects if message == "Directory actions"
    ]
    assert len(directory_menus) == 2
    assert all("Open a folder" in labels for labels in directory_menus)


def test_directory_scan_page_cap_bounds_fanout(
    settings: AtlasSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atlas.menu._DIRECTORY_SCAN_PAGE_LIMIT", 3)
    root = "https://example.com/root/"
    children = [f"{root}{index}/" for index in range(10)]
    seed_scan = WorkItem(
        url=root,
        final_url=root,
        host="example.com",
        kind=HubKind.dir,
        discovered_work_items=[
            WorkItem(url=url, host="example.com", kind=HubKind.dir) for url in children
        ],
    )
    actions = FakeActions(tmp_path)

    scans, limited = _scan_directory_roots(
        settings.model_copy(update={"dir_depth": 1}),
        actions,
        Console(file=StringIO()),
        seed_scan,
        [root],
    )

    assert limited is True
    assert len(scans) == 3
    assert actions.scan_calls == children[:2]


def test_partial_batch_command_exit_stays_in_menu_recovery(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    class Exit(RuntimeError):
        exit_code = 1

    prompts = FakePrompts(
        selects=[PlanMenuChoice.start, PlanRecoveryChoice.back],
    )
    actions = FakeActions(tmp_path)

    def fail_batch(*_args: object, **_kwargs: object) -> None:
        raise Exit()

    actions.run_batch = fail_batch  # type: ignore[method-assign]

    result = _batch_file_plan_flow(
        settings,
        actions,
        prompts,
        Console(
            file=StringIO(),
            theme=Theme(resolve_theme(AtlasThemeName.auto)),
        ),
        tmp_path / "urls.txt",
    )

    assert result == CompletionChoice.back
    assert any(message == "Continue" for message, _labels in prompts.seen_selects)


def test_menu_explicit_directory_action_uses_explorer(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "https://example.com/serveur/"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.dir,
            DirectoryExplorerChoice.visible_files,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        multi_selects=[
            [
                DirectoryEntry(
                    name="file.zip",
                    url="https://example.com/serveur/file.zip",
                    kind="file",
                )
            ]
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="example.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(url="https://example.com/serveur/sub/", host="example.com", kind=HubKind.dir),
            WorkItem(
                url="https://example.com/serveur/file.zip",
                host="example.com",
                kind=HubKind.file,
                file_extension=".zip",
            ),
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=StringIO()))

    assert actions.scan_calls == [seed_url]
    assert actions.batch_runs[0][1] == BatchKind.file


def test_menu_plan_renderer_hides_raw_backend_summary(tmp_path: Path) -> None:
    output = StringIO()
    options = FileDownloadOptions(
        url="https://example.com/archive.zip",
        output_dir=tmp_path,
    )
    route = EngineRoute(
        kind=HubKind.file,
        engine=EngineKind.native,
        reason="safe default for non-media URL",
        url=options.url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(
            route=route,
            output=tmp_path / "archive.zip",
            summary={
                "backend": "native",
                "backend_reason": "small file",
                "content_disposition": True,
                "trust_server_names": False,
                "probe": {
                    "content_type": "application/zip",
                    "content_length": 1_048_576,
                    "supports_ranges": True,
                    "etag": "raw-etag-that-should-not-render",
                },
            },
        ),
        options=options,
    )

    _print_menu_plan(Console(file=output), plan)

    rendered = output.getvalue()
    assert "Download plan" not in rendered
    assert "Files \u203a Download file" in rendered
    assert "Source" in rendered
    assert "Output" in rendered
    assert "Options" in rendered
    assert "Next" in rendered
    assert "direct file" not in rendered
    assert "Backend" not in rendered
    assert "Scheduler" not in rendered
    assert "1.0 MB" in rendered
    assert "Probe" not in rendered
    assert "Content Disposition" not in rendered
    assert "raw-etag" not in rendered


def test_video_plan_renderer_uses_single_summary_card(tmp_path: Path) -> None:
    try:
        configure_visuals(color=False, unicode=True, env={})
        output = StringIO()
        options = VideoDownloadOptions(
            url="https://www.youtube.com/watch?v=AICiFEiKM8c",
            output_dir=tmp_path,
            quality=QualityIntent.max,
            container=Container.mkv,
            format="313+251",
        )
        route = EngineRoute(
            kind=HubKind.video,
            engine=EngineKind.ytdlp,
            reason="user selected video",
            url=options.url,
            output_dir=tmp_path,
        )
        plan = HubExecutionPlan(
            route=route,
            preview=OptimizedDownloadPlan(
                route=route,
                output=tmp_path,
                summary={"container": "mkv"},
            ),
            options=options,
        )
        media = MediaInfo(
            title="How to Get a New Identity and Disappear.",
            uploader="Into the Shadows",
            extractor="YouTube",
            formats=[
                FormatInfo(
                    format_id="313",
                    ext="webm",
                    resolution="1080p",
                    vcodec="av01.0.08M.08",
                    acodec="none",
                ),
                FormatInfo(
                    format_id="251",
                    ext="webm",
                    resolution="audio only",
                    vcodec="none",
                    acodec="opus",
                ),
            ],
        )

        _print_menu_plan(
            Console(file=output, force_terminal=True, color_system=None, width=100),
            plan,
            media=media,
        )

        rendered = output.getvalue()
        assert "\u256d\u2500 Media \u203a Download video" in rendered
        assert "Title" in rendered
        assert "How to Get a New Identity and Disappear." in rendered
        assert "Source" in rendered
        assert "YouTube · Into the Shadows" in rendered
        assert "Quality" in rendered
        assert "Best quality · 1080p · AV1 + Opus" in rendered
        assert "Container" in rendered
        assert "MKV" in rendered
        assert "Output" in rendered
        assert "Options" in rendered
        assert "Choose exact format" in rendered
        assert "Choose format" not in rendered
        assert "Backend" not in rendered
        assert "Scheduler" not in rendered
    finally:
        reset_visuals()


def test_menu_plan_renderer_handles_default_mirror_timeout(tmp_path: Path) -> None:
    output = StringIO()
    options = DirectoryMirrorOptions(
        url="http://textfiles.com/directory.html",
        output_dir=tmp_path,
    )
    route = EngineRoute(
        kind=HubKind.dir,
        engine=EngineKind.wget2,
        reason="user selected dir",
        url=options.url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(
            route=route,
            output=tmp_path,
            summary={
                "backend": "wget2",
                "filter_urls": False,
                "verify_save_failed": False,
            },
        ),
        options=options,
    )

    _print_menu_plan(Console(file=output), plan)

    rendered = output.getvalue()
    assert "Files \u203a Browse directory" in rendered
    assert "Output" in rendered
    assert "Options" in rendered
    assert "Scope     same host" in rendered
    assert "Depth     2" in rendered
    assert "Filter Urls" not in rendered
    assert "Verify Save Failed" not in rendered


def test_menu_plan_titles_follow_selected_theme_styles(tmp_path: Path) -> None:
    output = StringIO()
    configure_visuals(
        theme=AtlasThemeName.light,
        color=True,
        unicode=True,
        env={},
    )
    try:
        themed = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            theme=Theme(resolve_theme(AtlasThemeName.light)),
        )
        options = FileDownloadOptions(url="https://example.com/archive.zip", output_dir=tmp_path)
        route = EngineRoute(
            kind=HubKind.file,
            engine=EngineKind.native,
            reason="direct file",
            url=options.url,
            output_dir=tmp_path,
        )
        plan = HubExecutionPlan(
            route=route,
            preview=OptimizedDownloadPlan(
                route=route,
                output=tmp_path / "archive.zip",
                summary={"backend": "native"},
            ),
            options=options,
        )
        failed_scan = WorkItem(
            url="https://example.com/files/",
            host="example.com",
            scan_status=ScanStatus.failed,
            error="TLS certificate verification failed",
        )

        _print_menu_plan(themed, plan)
        _print_batch_plan(
            themed,
            tmp_path / "urls.txt",
            BatchKind.auto,
            2,
            False,
            False,
            VideoCodecChoice.auto,
            AudioCodec.best,
            0,
        )
        _print_scan_failed(themed, failed_scan)

        rendered = output.getvalue()
        assert "\x1b[1;34matlas\x1b[0m" in rendered
        assert "\x1b[34mFiles \u203a Download file\x1b[0m" in rendered
        assert "\x1b[1;34m atlas Batch Plan " in rendered
        assert "\x1b[1;31m Scan failed " in rendered
        assert "\x1b[1;36m atlas Batch Plan " not in rendered
    finally:
        reset_visuals()


def test_url_scan_summary_shows_scan_warnings() -> None:
    output = StringIO()
    scan = WorkItem(
        url="https://example.com/serveur/",
        host="example.com",
        discovered_links=["https://example.com/serveur/readme.txt"],
        scan_warnings=["Python TLS verification failed; scanned using curl fallback."],
    )

    _print_url_scan_summary(Console(file=output), scan)

    rendered = output.getvalue()
    assert "Warnings" in rendered
    assert "curl fallback" in rendered


def test_url_scan_summary_is_compact_not_boxed() -> None:
    output = StringIO()
    scan = WorkItem(
        url="https://example.com/serveur/",
        final_url="https://example.com/serveur/",
        host="example.com",
        scan_type="directory-style HTML index",
        scan_counts={"links": 3, "files": 1, "folders": 2, "html": 0, "media": 0, "external": 0},
        scan_recommended_strategy="exact-list adaptive batch",
    )

    _print_url_scan_summary(Console(file=output), scan)

    rendered = output.getvalue()
    assert "Scan complete" in rendered
    assert "example.com/serveur/" in rendered
    assert "╭" not in rendered
    assert "Scan Complete" not in rendered


def test_directory_explorer_uses_compact_context_card_and_preview_sections() -> None:
    configure_visuals(plain=True, env={})
    output = StringIO()
    directory_index = DirectoryIndex(
        source_url="https://example.com/serveur/",
        host="example.com",
        entries=(
            DirectoryEntry(
                name="cours/",
                url="https://example.com/serveur/cours/",
                kind="directory",
                last_modified=datetime(2023, 12, 23, 6, 50),
            ),
            DirectoryEntry(
                name="readme.txt",
                url="https://example.com/serveur/readme.txt",
                kind="file",
                visible_size=1024,
            ),
        ),
    )

    _print_directory_explorer(
        Console(file=output),
        WorkItem(url="https://example.com/serveur/", scan_type="open directory index"),
        directory_index,
    )

    rendered = output.getvalue()
    assert "Browse Directory" in rendered
    assert "Location" in rendered
    assert "Navigation" in rendered
    assert "Scope" in rendered
    assert "Visible" in rendered
    assert "Visible size" in rendered
    assert "example.com/serveur/" in rendered
    assert "2023-12-23" in rendered
    assert "Files at this level (1)" in rendered
    assert "ctrl-c back" in rendered
    assert rendered.splitlines()[0].startswith("+")
    assert "Directory Explorer" not in rendered
    reset_visuals()


def test_directory_explorer_renders_remote_names_without_rich_markup_injection() -> None:
    output = StringIO()
    directory_index = DirectoryIndex(
        source_url="https://example.com/root/",
        host="example.com",
        entries=(
            DirectoryEntry(
                name="[link=https://evil.example]trusted[/link]/",
                url="https://example.com/root/trusted/",
                kind="directory",
            ),
            DirectoryEntry(
                name="\x1b]8;;https://evil.example\x07name\x1b]8;;\x07.txt",
                url="https://example.com/root/name.txt",
                kind="file",
            ),
        ),
    )

    _print_directory_explorer(
        Console(file=output, force_terminal=True, color_system="truecolor"),
        WorkItem(url=directory_index.source_url, scan_type="open directory index"),
        directory_index,
    )

    rendered = output.getvalue()
    assert "\x1b]8;;https://evil.example" not in rendered
    assert "[link=https://evil.example]trusted[/link]/" in rendered
    assert "name.txt" in rendered


def test_directory_explorer_shows_preview_note_and_warning_section() -> None:
    output = StringIO()
    entries = [
        DirectoryEntry(
            name="cours/",
            url="https://example.com/serveur/cours/",
            kind="directory",
            last_modified=datetime(2023, 12, 23, 6, 50),
            visible_size=9_663_676_416,
        )
    ]
    entries.extend(
        DirectoryEntry(
            name=f"file-{index}.pdf",
            url=f"https://example.com/serveur/file-{index}.pdf",
            kind="file",
            visible_size=2048,
        )
        for index in range(9)
    )

    _print_directory_explorer(
        Console(file=output, width=90),
        WorkItem(
            url="https://example.com/serveur/",
            scan_type="open directory index",
            scan_estimated_bytes=21_900_000_000,
            scan_warnings=[
                "Parent directory links skipped (no-parent policy)",
                "URL-encoded or spaced filenames detected",
            ],
        ),
        DirectoryIndex(
            source_url="https://example.com/serveur/",
            host="example.com",
            entries=tuple(entries),
        ),
    )

    rendered = output.getvalue()
    assert "Warnings" in rendered
    assert "Parent directory links skipped" in rendered
    assert "URL-encoded or spaced filenames detected" in rendered
    assert "showing first 8 of 9; choose files to search all" in rendered
    assert "9.0 GB" in rendered
    assert "~20.4 GB" in rendered


def test_directory_explorer_hides_file_preview_when_no_root_files_exist() -> None:
    output = StringIO()
    directory_index = DirectoryIndex(
        source_url="https://example.com/serveur/",
        host="example.com",
        entries=(
            DirectoryEntry(
                name="cours/",
                url="https://example.com/serveur/cours/",
                kind="directory",
            ),
        ),
    )

    _print_directory_explorer(
        Console(file=output),
        WorkItem(url="https://example.com/serveur/", scan_type="open directory index"),
        directory_index,
    )

    rendered = output.getvalue()
    assert "Folders (1)" in rendered
    assert "Files at this level" not in rendered


def test_directory_explorer_plain_mode_uses_ascii_footer_and_warning_bullets() -> None:
    try:
        configure_visuals(plain=True, env={})
        output = StringIO()
        directory_index = DirectoryIndex(
            source_url="https://example.com/serveur/",
            host="example.com",
            entries=(
                DirectoryEntry(
                    name="readme.txt",
                    url="https://example.com/serveur/readme.txt",
                    kind="file",
                ),
            ),
        )

        _print_directory_explorer(
            Console(file=output, width=80),
            WorkItem(
                url="https://example.com/serveur/",
                scan_type="open directory index",
                scan_warnings=["Parent directory links skipped"],
            ),
            directory_index,
        )

        rendered = output.getvalue()
        assert "up/down move" in rendered
        assert "ctrl-c back" in rendered
        assert "- Parent directory links skipped" in rendered
        assert "•" not in rendered
    finally:
        reset_visuals()


@pytest.mark.parametrize("width", [24, 48])
def test_directory_explorer_narrow_width_truncates_without_overflow(width: int) -> None:
    output = StringIO()
    directory_index = DirectoryIndex(
        source_url="https://ex.com/d/",
        host="ex.com",
        entries=(
            DirectoryEntry(
                name="a-very-long-filename-that-should-be-truncated-in-the-preview.pdf",
                url="https://ex.com/d/a-very-long-filename-that-should-be-truncated-in-the-preview.pdf",
                kind="file",
            ),
        ),
    )

    _print_directory_explorer(
        Console(file=output, width=width),
        WorkItem(url="https://ex.com/d/", scan_type="open directory index"),
        directory_index,
    )

    rendered = output.getvalue()
    assert "Files at this level (1)" in rendered
    assert max(len(line) for line in rendered.splitlines()) <= width


def test_directory_explorer_redraw_emits_clear_sequence_when_menu_owns_screen() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=80)
    console._atlas_menu_redraw = True  # type: ignore[attr-defined]
    calls: list[bool] = []
    original_clear = console.clear

    def recording_clear(*, home: bool = True) -> None:
        calls.append(home)
        original_clear(home=home)

    console.clear = recording_clear  # type: ignore[method-assign]

    _print_directory_explorer(
        console,
        WorkItem(url="https://example.com/serveur/", scan_type="open directory index"),
        DirectoryIndex(
            source_url="https://example.com/serveur/",
            host="example.com",
            entries=(),
        ),
    )

    assert calls == [True]


def test_menu_url_scan_failure_shows_recovery_actions_without_discovered_download(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "https://example.com/serveur/"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.url_scan,
            ScanFailedChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="example.com",
        scan_status=ScanStatus.failed,
        scan_type="failed scan",
        scan_errors=[
            {
                "code": "tls_failed",
                "message": "TLS certificate verification failed",
                "url": seed_url,
                "recoverable": True,
            }
        ],
        error="TLS certificate verification failed",
    )
    output = StringIO()

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=output))

    rendered = output.getvalue()
    assert "Scan failed" in rendered
    assert "Scan complete" not in rendered
    assert "Download discovered files" not in rendered
    labels = next(labels for message, labels in prompts.seen_selects if message == "Scan failed")
    assert labels == [
        "Retry scan",
        "Run network diagnostics",
        "Plan bounded mirror without scan",
        "Error details",
        "Back",
    ]


def test_menu_url_scan_empty_shows_only_empty_state_actions(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "https://example.com/empty"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.url_scan,
            ScanEmptyChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="example.com",
        scan_status=ScanStatus.empty,
        scan_type="empty scan",
        scan_counts={"links": 0, "files": 0, "folders": 0, "html": 0, "media": 0, "external": 0},
        scan_errors=[
            {
                "code": "no_links",
                "message": "No links found in fetched document",
                "url": seed_url,
                "recoverable": True,
            }
        ],
    )
    output = StringIO()

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=output))

    rendered = output.getvalue()
    assert "No links found" in rendered
    assert "Scan complete" not in rendered
    labels = next(labels for message, labels in prompts.seen_selects if message == "No links found")
    assert labels == [
        "Retry scan",
        "Plan offline website mirror",
        "Download this URL only",
        "Back",
    ]
    assert "Download discovered files" not in labels
    assert "Choose discovered files" not in labels


def test_menu_video_flow_detects_explicit_playlist(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.video,
            HubKind.audio,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["https://www.youtube.com/playlist?list=PL123"],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    first_options, first_kind = actions.planned[0]
    assert isinstance(first_options, AudioDownloadOptions)
    assert first_options.playlist is True
    assert first_kind == HubKind.audio
    prompt = next(
        labels for message, labels in prompts.seen_selects if message == "Playlist detected"
    )
    assert "Download playlist as audio" in prompt


def test_plan_loop_can_show_formats_before_start(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.video,
            PlanMenuChoice.formats,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["https://www.youtube.com/watch?v=abc"],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.formats_runs == ["https://www.youtube.com/watch?v=abc"]
    assert actions.executed[0].options.dry_run is True


def test_plan_execution_failure_retries_without_leaving_menu(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    class FlakyExecutionActions(FakeActions):
        def execute_plan(self, plan: HubExecutionPlan) -> list[Path]:
            self.executed.append(plan)
            if len(self.executed) == 1:
                raise AtlasError(
                    "Request failed: https://example.com/archive.zip?X-Goog-Signature=TOPSECRET"
                )
            return [self.output_dir / "download.bin"]

    output = StringIO()
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.file,
            PlanMenuChoice.start,
            PlanRecoveryChoice.retry,
            CompletionChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["https://example.com/archive.zip"],
    )
    actions = FlakyExecutionActions(tmp_path)

    run_interactive_menu(
        settings,
        actions,
        prompts=prompts,
        console=Console(file=output, width=100),
    )

    rendered = output.getvalue()
    assert len(actions.executed) == 2
    assert "Needs attention" in rendered
    assert "Download interrupted" in rendered
    assert "TOPSECRET" not in rendered
    assert "<redacted>" in rendered
    recovery_prompt = next(
        labels for message, labels in prompts.seen_selects if message == "Continue"
    )
    assert recovery_prompt == [
        "Try again",
        "Customize plan",
        "Run diagnostics",
        "Back to menu",
        "Quit",
    ]


def test_menu_cancel_url_goes_back_without_execution(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[MainMenuChoice.audio, MainMenuChoice.quit],
        texts=[None],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.planned == []
    assert actions.executed == []


def test_menu_video_customize_uses_arrow_overlay_choices(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "custom-out"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.video,
            PlanMenuChoice.customize,
            "quality",
            QualityIntent.compatible,
            ResolutionChoice.r1080,
            "format",
            Container.mp4,
            VideoCodecChoice.h264,
            "output",
            "cookies",
            "safari",
            "subtitles",
            SubtitleMode.manual,
            "back",
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[
            "https://www.youtube.com/watch?v=abc",
            str(output_dir),
            "en",
        ],
        confirms=[True],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    executed_options = actions.executed[0].options
    assert isinstance(executed_options, VideoDownloadOptions)
    assert executed_options.quality == QualityIntent.compatible
    assert executed_options.resolution == ResolutionChoice.r1080
    assert executed_options.container == Container.mp4
    assert executed_options.video_codec == VideoCodecChoice.h264
    assert executed_options.output_dir == output_dir
    assert executed_options.browser_cookies == "safari"
    assert executed_options.subtitle_mode == SubtitleMode.manual
    assert executed_options.sub_lang == "en"
    assert executed_options.embed_subs is True
    customize_prompt = next(
        labels for message, labels in prompts.seen_selects if message == "Customize"
    )
    assert customize_prompt == [
        "Quality",
        "Format",
        "Details",
        "yt-dlp format",
        "Engine",
        "Filters",
        "Sections",
        "Playlist",
        "Metadata",
        "Output",
        "Cookies",
        "Subtitles",
        "Back",
    ]


def test_menu_audio_customize_selects_codec_and_quality(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.audio,
            PlanMenuChoice.customize,
            "audio-format",
            AudioCodec.mp3,
            "back",
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[
            "https://www.youtube.com/watch?v=abc",
            "4",
        ],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    executed_options = actions.executed[0].options
    assert isinstance(executed_options, AudioDownloadOptions)
    assert executed_options.codec == AudioCodec.mp3
    assert executed_options.quality == 4
    assert any(message == "Codec" and "mp3" in labels for message, labels in prompts.seen_selects)


def test_menu_video_customize_exposes_selection_sections_and_engine(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.video,
            PlanMenuChoice.customize,
            "video-details",
            HdrChoice.prefer,
            FpsChoice.f60,
            "custom-format",
            "media-engine",
            DownloadEngineChoice.native,
            "media-selection",
            "media-sections",
            "back",
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[
            "https://www.youtube.com/watch?v=abc",
            "bv*+ba/b",
            "8",
            "4",
            "2M",
            "11",
            "12",
            "5",
            "8",
            "http:1 | fragment:linear=1::10",
            "512K",
            "64K",
            "10M",
            "12.5",
            "127.0.0.1",
            "chrome",
            "youtube:player_client=android",
            "0.5",
            "socks5://127.0.0.1:9050",
            "3",
            "duration>?60",
            "view_count<10",
            "20240102",
            "",
            "20240101",
            "10M",
            "1G",
            "intro | *10:15-inf",
            "sponsor",
            "selfpromo",
            "[SB] %(category_names)l",
            "https://sb.example",
        ],
        confirms=[
            False,
            False,
            True,
            True,
            True,
            True,
            True,
            True,
        ],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    executed_options = actions.executed[0].options
    assert isinstance(executed_options, VideoDownloadOptions)
    assert executed_options.hdr == HdrChoice.prefer
    assert executed_options.fps == FpsChoice.f60
    assert executed_options.format == "bv*+ba/b"
    assert executed_options.use_aria2 is False
    assert executed_options.download_engine == DownloadEngineChoice.native
    assert executed_options.connections == 8
    assert executed_options.splits == 4
    assert executed_options.chunk_size == "2M"
    assert executed_options.retries == 11
    assert executed_options.fragment_retries == 12
    assert executed_options.file_access_retries == 5
    assert executed_options.concurrent_fragments == 8
    assert executed_options.retry_sleep == ["http:1", "fragment:linear=1::10"]
    assert executed_options.skip_unavailable_fragments is False
    assert executed_options.rate_limit == "512K"
    assert executed_options.throttled_rate == "64K"
    assert executed_options.http_chunk_size == "10M"
    assert executed_options.socket_timeout == 12.5
    assert executed_options.source_address == "127.0.0.1"
    assert executed_options.impersonate == "chrome"
    assert executed_options.extractor_args == ["youtube:player_client=android"]
    assert executed_options.sleep == 0.5
    assert executed_options.proxy == "socks5://127.0.0.1:9050"
    assert executed_options.match_filters == ["duration>?60"]
    assert executed_options.break_match_filters == ["view_count<10"]
    assert executed_options.max_downloads == 3
    assert executed_options.break_on_existing is True
    assert executed_options.break_on_reject is True
    assert executed_options.break_per_input is True
    assert executed_options.date == "20240102"
    assert executed_options.date_before is None
    assert executed_options.date_after == "20240101"
    assert executed_options.min_filesize == "10M"
    assert executed_options.max_filesize == "1G"
    assert executed_options.reject_live is True
    assert executed_options.reject_upcoming is True
    assert executed_options.live_from_start is True
    assert executed_options.download_sections == ["intro", "*10:15-inf"]
    assert executed_options.sponsorblock_mark == ["sponsor"]
    assert executed_options.sponsorblock_remove == ["selfpromo"]
    assert executed_options.sponsorblock_chapter_title == "[SB] %(category_names)l"
    assert executed_options.sponsorblock_api == "https://sb.example"


def test_menu_file_customize_exposes_aria2_session_metalink_and_stats(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.file,
            PlanMenuChoice.customize,
            "aria2-session",
            "metalink",
            MetalinkPreferredProtocol.https,
            "server-stats",
            Aria2UriSelector.adaptive,
            "back",
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[
            "https://example.com/release.meta4",
            str(tmp_path / "aria2.session"),
            str(tmp_path / "aria2.next"),
            "30",
            "en-US",
            "macos",
            "us",
            "https://mirrors.example/releases/",
            str(tmp_path / "servers.in"),
            str(tmp_path / "servers.out"),
            "3600",
        ],
        confirms=[True, True, False],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    executed_options = actions.executed[0].options
    assert isinstance(executed_options, FileDownloadOptions)
    assert executed_options.input_file == tmp_path / "aria2.session"
    assert executed_options.save_session == tmp_path / "aria2.next"
    assert executed_options.save_session_interval == 30
    assert executed_options.metalink is True
    assert executed_options.force_metalink is True
    assert executed_options.metalink_preferred_protocol == MetalinkPreferredProtocol.https
    assert executed_options.metalink_language == "en-US"
    assert executed_options.metalink_os == "macos"
    assert executed_options.metalink_location == "us"
    assert executed_options.metalink_base_uri == "https://mirrors.example/releases/"
    assert executed_options.metalink_enable_unique_protocol is False
    assert executed_options.server_stat_if == tmp_path / "servers.in"
    assert executed_options.server_stat_of == tmp_path / "servers.out"
    assert executed_options.server_stat_timeout == 3600
    assert executed_options.uri_selector == Aria2UriSelector.adaptive


def test_customize_rejects_invalid_updates_and_keeps_prior_file_options(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    options = build_file_options(settings, "https://example.com/archive.zip")
    output = StringIO()
    prompts = FakePrompts(
        selects=["file-format", "back"],
        texts=["archive.zip", "not-a-checksum"],
        confirms=[None],
    )

    updated = _customize_options(
        prompts,
        options,
        console=Console(file=output, force_terminal=True, color_system=None),
    )

    assert isinstance(updated, FileDownloadOptions)
    assert updated.filename is None
    assert updated.checksum is None
    assert "Not applied: checksum: checksum must look like sha256:<hex-digest>" in output.getvalue()


def test_file_format_customize_cancel_keeps_existing_values(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    options = build_file_options(settings, "https://example.com/archive.zip").model_copy(
        update={"filename": "original.zip", "checksum": "sha256:aa"}
    )

    updated = _apply_customize_overlay(
        FakePrompts(selects=[], texts=[None, None], confirms=[None]),
        options,
        "file-format",
    )

    assert isinstance(updated, FileDownloadOptions)
    assert updated.filename == "original.zip"
    assert updated.checksum == "sha256:aa"


def test_menu_playlist_can_map_to_audio_playlist(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.playlist,
            BatchKind.audio,
            PlanMenuChoice.start,
            CompletionChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["https://www.youtube.com/playlist?list=PL123"],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    planned_options, planned_kind = actions.planned[0]
    assert planned_kind == HubKind.audio
    assert planned_options.playlist is True
    assert actions.executed[0].options.playlist is True
    completion_prompt = next(
        labels
        for message, labels in prompts.seen_selects
        if message == "Next" and "Show in folder" in labels
    )
    assert completion_prompt == [
        "Show in folder",
        "Open file",
        "Extract another",
        "Back to menu",
        "Quit",
    ]


def test_completion_actions_are_platform_neutral_and_report_launch_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = tmp_path / "file.bin"
    saved.write_bytes(b"x")
    calls: list[list[str]] = []
    monkeypatch.setattr("atlas.menu.sys.platform", "linux")
    monkeypatch.setattr(
        "atlas.menu.shutil.which",
        lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
    )
    monkeypatch.setattr(
        "atlas.menu.subprocess.run",
        lambda command, **_kwargs: calls.append(command) or type("Result", (), {"returncode": 0})(),
    )

    assert _reveal_path(saved) is None
    assert _open_path(saved) is None
    assert calls == [
        ["/usr/bin/xdg-open", str(tmp_path)],
        ["/usr/bin/xdg-open", str(saved)],
    ]

    monkeypatch.setattr("atlas.menu.shutil.which", lambda _name: None)
    output = StringIO()
    prompts = FakePrompts(selects=[CompletionChoice.back])
    _completion_loop(
        prompts,
        [saved],
        console=Console(file=output),
    )
    labels = next(labels for message, labels in prompts.seen_selects if message == "Next")
    assert "Show in folder" not in labels
    assert "Open file" not in labels


def test_completion_prefers_final_media_file_over_stream_fragments(tmp_path: Path) -> None:
    options = VideoDownloadOptions(
        url="https://www.youtube.com/watch?v=abc",
        output_dir=tmp_path,
        container=Container.mp4,
    )
    route = EngineRoute(
        kind=HubKind.video,
        engine=EngineKind.ytdlp,
        reason="user selected video",
        url=options.url,
        output_dir=tmp_path,
    )
    plan = HubExecutionPlan(
        route=route,
        preview=OptimizedDownloadPlan(
            route=route,
            output=tmp_path,
            summary={"container": "mp4"},
        ),
        options=options,
    )
    paths = [
        tmp_path / "Example.f298.mp4",
        tmp_path / "Example.f140.m4a",
        tmp_path / "Example.mp4",
    ]

    assert _primary_saved_path(paths, plan=plan) == tmp_path / "Example.mp4"


def test_video_completion_summary_uses_saved_file_card(tmp_path: Path) -> None:
    try:
        configure_visuals(color=False, unicode=True, env={})
        saved = tmp_path / "2025-03-09 - How to Get a New Identity and Disappear. [AICiFEiKM8c].mp4"
        saved.write_bytes(b"x" * 133_500)
        options = VideoDownloadOptions(
            url="https://www.youtube.com/watch?v=AICiFEiKM8c",
            output_dir=tmp_path,
            container=Container.mp4,
            video_codec=VideoCodecChoice.h264,
        )
        route = EngineRoute(
            kind=HubKind.video,
            engine=EngineKind.ytdlp,
            reason="user selected video",
            url=options.url,
            output_dir=tmp_path,
        )
        plan = HubExecutionPlan(
            route=route,
            preview=OptimizedDownloadPlan(
                route=route,
                output=saved,
                summary={"container": "mp4"},
            ),
            options=options,
        )
        output = StringIO()

        _print_completion_summary(
            Console(file=output, force_terminal=True, color_system=None, width=180),
            [saved],
            saved,
            plan=plan,
        )

        rendered = output.getvalue()
        assert "\u256d\u2500 Download Complete" in rendered
        assert "\u2713 Saved" in rendered
        assert "2025-03-09 - How to Get a New Identity and Disappear." in rendered
        assert "[AICiFEiKM8c].mp4" in rendered
        assert "Details" in rendered
        assert "Format" in rendered
        assert "MP4" in rendered
        assert "Video" in rendered
        assert "H.264" in rendered
    finally:
        reset_visuals()


def test_menu_playlist_customize_can_multiselect_playlist_items(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.playlist,
            BatchKind.audio,
            PlanMenuChoice.customize,
            "playlist-range",
            OrganizeMode.playlist,
            "selected",
            "back",
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        multi_selects=[["1", "3", "10"]],
        texts=["https://www.youtube.com/playlist?list=PL123"],
        confirms=[True],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    executed_options = actions.executed[0].options
    assert isinstance(executed_options, AudioDownloadOptions)
    assert executed_options.playlist is True
    assert executed_options.playlist_items == "1,3,10"
    assert executed_options.playlist_start is None
    assert executed_options.playlist_end is None
    item_prompt = next(
        labels
        for message, labels in prompts.seen_multi_selects
        if message == "Choose playlist items"
    )
    assert item_prompt[:3] == ["Item 1", "Item 2", "Item 3"]


def test_menu_customize_prints_changed_options_diff(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.playlist,
            BatchKind.audio,
            PlanMenuChoice.customize,
            "playlist-range",
            OrganizeMode.playlist,
            "selected",
            "back",
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        multi_selects=[["1", "3", "10"]],
        texts=["https://www.youtube.com/playlist?list=PL123"],
        confirms=[True],
    )
    actions = FakeActions(tmp_path)
    output = StringIO()

    run_interactive_menu(
        settings,
        actions,
        prompts=prompts,
        console=Console(file=output, force_terminal=True, color_system=None, width=140),
    )

    rendered = output.getvalue()
    assert "Changed Options" in rendered
    assert "Playlist Items" in rendered
    assert "unset -> 1,3,10" in rendered


def test_menu_batch_customize_and_dry_run(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    batch_file = tmp_path / "urls.txt"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.url_file,
            PlanMenuChoice.customize,
            "kind",
            BatchKind.video,
            "concurrency",
            "codecs",
            VideoCodecChoice.hevc,
            AudioCodec.mp3,
            "sites",
            "back",
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[str(batch_file), "3", "4"],
        confirms=[True],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.batch_runs == [
        (
            batch_file,
            BatchKind.video,
            3,
            True,
            False,
            VideoCodecChoice.hevc,
            AudioCodec.mp3,
            4,
            True,
        )
    ]
    assert any(message == "Batch queue" for message, _labels in prompts.seen_selects)
    source_prompt = next(labels for message, labels in prompts.seen_selects if message == "Batch")
    assert source_prompt == [
        "Paste URL and scan",
        "Use URL file",
        "Paste multiple URLs",
        "Playlist as batch",
        "Resume session",
        "Retry failed",
        "Inspect session",
        "Export URLs",
        "Back",
        "Quit",
    ]


def test_menu_batch_pasted_urls_creates_generated_queue(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.pasted_urls,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[
            "https://example.com/one.zip\n# comment\nhttps://example.com/two.mp4,"
            " https://example.org/three.txt"
        ],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert len(actions.batch_runs) == 1
    queue_path = actions.batch_runs[0][0]
    assert queue_path.parent == settings.output_dir / ".atlas" / "menu"
    assert queue_path.read_text(encoding="utf-8").splitlines() == [
        "https://example.com/one.zip",
        "https://example.com/two.mp4",
        "https://example.org/three.txt",
    ]
    assert queue_path.stat().st_mode & 0o777 == 0o600
    assert queue_path.parent.stat().st_mode & 0o777 == 0o700


def test_menu_batch_playlist_builds_media_playlist_plan(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.playlist,
            HubKind.audio,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["https://www.youtube.com/playlist?list=PL123"],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    planned_options, planned_kind = actions.planned[0]
    assert planned_kind == HubKind.audio
    assert isinstance(planned_options, AudioDownloadOptions)
    assert planned_options.playlist is True
    assert actions.executed[0].options.dry_run is True


def test_menu_batch_resume_and_retry_sessions(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    retry_path = tmp_path / "retry.atlas.json"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.resume,
            PlanMenuChoice.dry_run,
            MainMenuChoice.batch,
            BatchSourceChoice.retry,
            PlanMenuChoice.start,
            CompletionChoice.back,
            MainMenuChoice.quit,
        ],
        texts=["latest", str(retry_path)],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.resumed_sessions == [(None, True)]
    assert actions.retried_sessions == [(retry_path, False)]


def test_menu_batch_inspects_saved_session(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    retry_path = tmp_path / "retry.atlas.json"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.inspect,
            PlanMenuChoice.start,
            "back",
            MainMenuChoice.quit,
        ],
        texts=[str(retry_path)],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.inspected_sessions == [retry_path]


def test_menu_top_level_session_operator_actions(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    retry_path = tmp_path / "retry.atlas.json"
    export_path = tmp_path / "failed.txt"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.resume,
            PlanMenuChoice.dry_run,
            MainMenuChoice.retry,
            PlanMenuChoice.start,
            CompletionChoice.back,
            MainMenuChoice.inspect,
            PlanMenuChoice.start,
            "back",
            MainMenuChoice.export_failed,
            PlanMenuChoice.start,
            "back",
            MainMenuChoice.quit,
        ],
        texts=[
            "latest",
            str(retry_path),
            str(retry_path),
            str(retry_path),
            str(export_path),
        ],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.resumed_sessions == [(None, True)]
    assert actions.retried_sessions == [(retry_path, False)]
    assert actions.inspected_sessions == [retry_path]
    assert actions.exported_sessions == [(retry_path, export_path)]


def test_menu_batch_url_scan_builds_recursive_directory_plan(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "http://textfiles.com/directory.html"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.url_scan,
            BatchUrlScanChoice.recursive,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="textfiles.com",
        discovered_links=[
            "http://textfiles.com/bbs/old-bbs-list.txt",
            "http://textfiles.com/etext/index.html",
            "http://example.com/offsite.zip",
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    planned_options, planned_kind = actions.planned[0]
    assert planned_kind == HubKind.dir
    assert isinstance(planned_options, DirectoryMirrorOptions)
    assert planned_options.url == seed_url
    assert planned_options.adaptive is True
    assert planned_options.no_parent is True
    assert planned_options.span_hosts is False
    assert planned_options.domains == "textfiles.com,www.textfiles.com"
    assert planned_options.wait == 0.5
    assert planned_options.random_wait is True
    assert planned_options.timeout == 60.0
    assert planned_options.tries == 5
    assert planned_options.continue_download is True
    assert actions.executed[0].options.dry_run is True
    assert any(message == "Actions" for message, _labels in prompts.seen_selects)


def test_menu_batch_url_scan_can_choose_discovered_folder(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "http://textfiles.com/directory.html"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.url_scan,
            DirectoryExplorerChoice.folder,
            DirectoryEntry(
                name="bbs/",
                url="http://textfiles.com/bbs/",
                kind="directory",
            ),
            DirectoryExplorerChoice.visible_files,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        multi_selects=[
            [
                DirectoryEntry(
                    name="old-bbs-list.txt",
                    url="http://textfiles.com/bbs/old-bbs-list.txt",
                    kind="file",
                )
            ]
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="textfiles.com",
        discovered_links=[
            "http://textfiles.com/bbs/",
            "http://textfiles.com/bbs/old-bbs-list.txt",
        ],
    )
    actions.scan_items["http://textfiles.com/bbs/"] = WorkItem(
        url="http://textfiles.com/bbs/",
        host="textfiles.com",
        scan_type="directory-style HTML index",
        discovered_work_items=[
            WorkItem(
                url="http://textfiles.com/bbs/old-bbs-list.txt",
                host="textfiles.com",
                kind=HubKind.file,
                file_extension=".txt",
            )
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.scan_calls == [seed_url, "http://textfiles.com/bbs/"]
    queue_path, kind, _concurrency, _allow_sites, allow_dirs, *_rest = actions.batch_runs[0]
    assert kind == BatchKind.file
    assert allow_dirs is False
    assert queue_path.read_text(encoding="utf-8").splitlines() == [
        "http://textfiles.com/bbs/old-bbs-list.txt"
    ]
    folder_prompt = next(
        labels for message, labels in prompts.seen_selects if message == "Open folder"
    )
    assert any("bbs/" in label for label in folder_prompt)


def test_menu_batch_url_scan_files_only_rejects_html(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "http://textfiles.com/directory.html"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.url_scan,
            BatchUrlScanChoice.direct_links,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="textfiles.com",
        discovered_links=[
            "http://textfiles.com/bbs/old-bbs-list.txt",
            "http://textfiles.com/etext/index.html",
            "http://example.com/offsite.zip",
        ],
        scan_counts={
            "links": 3,
            "files": 1,
            "folders": 0,
            "html": 1,
            "media": 0,
            "external": 1,
        },
        scan_recommended_mode="Recursive directory mirror with HTML preservation",
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert len(actions.batch_runs) == 1
    queue_path, kind, _concurrency, allow_sites, allow_dirs, *_rest = actions.batch_runs[0]
    assert kind == BatchKind.auto
    assert allow_sites is False
    assert allow_dirs is False
    assert queue_path.read_text(encoding="utf-8").splitlines() == [
        "http://textfiles.com/bbs/old-bbs-list.txt"
    ]
    choice_labels = next(labels for message, labels in prompts.seen_selects if message == "Actions")
    assert "Download discovered files" in choice_labels
    assert "Choose discovered files" in choice_labels


def test_menu_batch_url_scan_can_choose_discovered_files(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    seed_url = "http://textfiles.com/directory.html"
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.batch,
            BatchSourceChoice.url_scan,
            BatchUrlScanChoice.selected_files,
            PlanMenuChoice.dry_run,
            PlanMenuChoice.back,
            MainMenuChoice.quit,
        ],
        multi_selects=[
            [
                "http://textfiles.com/bbs/old-bbs-list.txt",
                "http://textfiles.com/music/theme.mp3",
            ]
        ],
        texts=[seed_url],
    )
    actions = FakeActions(tmp_path)
    actions.scan_items[seed_url] = WorkItem(
        url=seed_url,
        host="textfiles.com",
        discovered_links=[
            "http://textfiles.com/bbs/old-bbs-list.txt",
            "http://textfiles.com/etext/index.html",
            "http://textfiles.com/music/theme.mp3",
            "http://example.com/offsite.zip",
        ],
    )

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert len(actions.batch_runs) == 1
    queue_path = actions.batch_runs[0][0]
    assert queue_path.read_text(encoding="utf-8").splitlines() == [
        "http://textfiles.com/bbs/old-bbs-list.txt",
        "http://textfiles.com/music/theme.mp3",
    ]
    file_prompt = next(
        labels
        for message, labels in prompts.seen_multi_selects
        if message == "Choose discovered files"
    )
    assert "/bbs/old-bbs-list.txt" in file_prompt
    assert "/music/theme.mp3" in file_prompt
    assert all("offsite.zip" not in label for label in file_prompt)


def test_menu_advanced_backend_command(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    prompts = FakePrompts(
        selects=[
            MainMenuChoice.advanced,
            BackendTool.ytdlp,
            "back",
            MainMenuChoice.quit,
        ],
        texts=['--format "bv*+ba/b" https://example.com/watch?v=abc'],
        confirms=[True],
    )
    actions = FakeActions(tmp_path)

    run_interactive_menu(settings, actions, prompts=prompts, console=Console(file=None))

    assert actions.backend_runs == [
        (
            BackendTool.ytdlp,
            ["--format", "bv*+ba/b", "https://example.com/watch?v=abc"],
            True,
        )
    ]


def test_menu_site_customize_exposes_all_site_overlays(settings: AtlasSettings) -> None:
    options = build_site_options(settings, "https://example.com/docs")

    labels = [choice.label for choice in _customize_choices(options)]

    assert labels == [
        "Backend",
        "Output",
        "Basics",
        "Scope",
        "Discovery",
        "Paths",
        "Parsers",
        "HTTP",
        "Cookies/auth",
        "TLS/OCSP",
        "Signatures",
        "Network",
        "Archive",
        "Bounds",
        "Adaptive",
        "Back",
    ]


def test_menu_site_customize_sets_wget2_options(
    settings: AtlasSettings,
    tmp_path: Path,
) -> None:
    options = build_site_options(settings, "https://example.com/docs")

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[],
            texts=["4", "0.25"],
            confirms=[True, False, False, True],
        ),
        options,
        "site-format",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.depth == 4
    assert options.page_requisites is False
    assert options.convert_links is False
    assert options.wait == 0.25
    assert options.spider is True

    options = _apply_customize_overlay(
        FakePrompts(
            selects=["same-domain-www"],
            confirms=[True],
        ),
        options,
        "site-scope",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.span_hosts is True
    assert options.domains == "example.com,www.example.com"
    assert options.no_parent is True

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[],
            texts=[
                "html,png",
                "tmp",
                "example.com",
                "ads.example.com",
                "/docs",
                "/private",
                ".*\\.html$",
                "logout",
                "text/html",
                "img/data-src",
                "a/href",
            ],
            confirms=[False, False, False, True, True],
        ),
        options,
        "site-discovery",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.robots is False
    assert options.follow_sitemaps is False
    assert options.no_parent is False
    assert options.filter_urls is True
    assert options.ignore_case is True
    assert options.follow_tags == "img/data-src"
    assert options.ignore_tags == "a/href"

    options = _apply_customize_overlay(
        FakePrompts(
            selects=["enabled", "disabled", "enabled", DownloadAttrMode.strip_path],
            texts=["2", "home.html", "3", "windows"],
            confirms=[True, True, False, True, True, True, True, True, True],
        ),
        options,
        "site-layout",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.directories is True
    assert options.host_directories is False
    assert options.protocol_directories is True
    assert options.cut_dirs == 2
    assert options.adjust_extension is True
    assert options.continue_download is True
    assert options.overwrite is False
    assert options.convert_file_only is True
    assert options.cut_url_get_vars is True
    assert options.cut_file_get_vars is True
    assert options.keep_extension is True
    assert options.unlink is True
    assert options.backups == 3
    assert options.backup_converted is True
    assert options.download_attr == DownloadAttrMode.strip_path

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[],
            texts=[str(tmp_path / "urls.txt"), "https://example.com/"],
            confirms=[True, True, True, True, True, True, True],
        ),
        options,
        "site-parser",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.input_file == tmp_path / "urls.txt"
    assert options.input_file_only is True
    assert options.base == "https://example.com/"
    assert options.force_html is True
    assert options.force_css is True
    assert options.force_sitemap is True
    assert options.force_atom is True
    assert options.force_rss is True
    assert options.force_metalink is True

    options = _apply_customize_overlay(
        FakePrompts(
            selects=["disabled"],
            texts=[
                "AtlasTest/1.0",
                "X-Test: yes | Accept-Language: en",
                "https://referrer.example/",
                "br",
                "POST",
                "payload",
                str(tmp_path / "body.txt"),
                "legacy=1",
                str(tmp_path / "post.txt"),
                "500,502",
                "10M",
                "1M",
                "1024",
            ],
            confirms=[True, True, True, True, True],
        ),
        options,
        "site-http",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.user_agent == "AtlasTest/1.0"
    assert options.headers == ("X-Test: yes", "Accept-Language: en")
    assert options.cache is False
    assert options.no_compression is True
    assert options.content_on_error is True
    assert options.save_headers is True
    assert options.server_response is True
    assert options.ignore_length is True
    assert options.save_content_on == "500,502"

    options = _apply_customize_overlay(
        FakePrompts(
            selects=["enabled", "safari", "enabled", "disabled"],
            texts=[
                str(tmp_path / "cookies.txt"),
                str(tmp_path / "saved-cookies.txt"),
                "public_suffixes.dat",
                str(tmp_path / "netrc"),
                "alice",
                "proxy-user",
            ],
            secrets=["secret", "proxy-secret"],
            confirms=[True],
        ),
        options,
        "site-cookies",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.cookies is True
    assert options.browser_cookies == "safari"
    assert options.load_cookies == tmp_path / "cookies.txt"
    assert options.save_cookies == tmp_path / "saved-cookies.txt"
    assert options.keep_session_cookies is True
    assert options.netrc is True
    assert options.proxy is False
    assert options.http_user == "alice"
    assert options.http_password == "secret"
    assert options.proxy_password == "proxy-secret"

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[
                HttpsEnforceMode.hard,
                "enabled",
                "disabled",
                "disabled",
                CertificateType.pem,
                CertificateType.der,
                "enabled",
                "disabled",
                "disabled",
                "enabled",
                "enabled",
                "enabled",
                "enabled",
            ],
            texts=[
                str(tmp_path / "hsts.db"),
                str(tmp_path / "ca.pem"),
                str(tmp_path / "ca-dir"),
                str(tmp_path / "client.pem"),
                str(tmp_path / "client.key"),
                str(tmp_path / "revocations.pem"),
                "TLSv1_2",
                str(tmp_path / "ocsp.db"),
                "http://ocsp.example/",
                str(tmp_path / "tls-sessions.db"),
                "12",
            ],
            confirms=[True, True],
        ),
        options,
        "site-tls",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.https_only is True
    assert options.https_enforce == HttpsEnforceMode.hard
    assert options.hsts is True
    assert options.check_certificate is False
    assert options.check_hostname is False
    assert options.certificate_type == CertificateType.pem
    assert options.private_key_type == CertificateType.der
    assert options.ocsp is True
    assert options.ocsp_date is False
    assert options.ocsp_nonce is False
    assert options.ocsp_stapling is True
    assert options.tls_false_start is True
    assert options.tls_resume is True
    assert options.http2 is True
    assert options.http2_only is True
    assert options.http2_request_window == 12

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[VerifySigMode.no_fail],
            texts=["asc,sig", str(tmp_path / "gnupg")],
            confirms=[True],
        ),
        options,
        "site-gpg",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.verify_sig == VerifySigMode.no_fail
    assert options.signature_extensions == "asc,sig"
    assert options.gnupg_homedir == tmp_path / "gnupg"
    assert options.verify_save_failed is True

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[PreferFamily.ipv6, "disabled", "disabled"],
            texts=[
                "127.0.0.1",
                "lo0",
                str(tmp_path / "dns-cache.txt"),
                "7",
                "3",
                "2.5",
                "429,503",
                "4",
                "9",
                "1",
                "2",
                "3",
            ],
            confirms=[True, False, True, True, True],
        ),
        options,
        "site-network",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.retry_connrefused is True
    assert options.inet4_only is False
    assert options.inet6_only is True
    assert options.bind_interface == "lo0"
    assert options.prefer_family == PreferFamily.ipv6
    assert options.dns_cache is False
    assert options.tcp_fastopen is False
    assert options.max_threads == 7
    assert options.tries == 3
    assert options.waitretry == 2.5
    assert options.retry_on_http_error == "429,503"
    assert options.timeout == 9
    assert options.random_wait is True
    assert options.timestamping is True

    options = _apply_customize_overlay(
        FakePrompts(
            selects=["enabled"],
            texts=[str(tmp_path / "archive.warc.gz"), "1G"],
            confirms=[True, False],
        ),
        options,
        "site-archive",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.warc_file == tmp_path / "archive.warc.gz"
    assert options.warc_compression is True
    assert options.warc_cdx is True
    assert options.warc_max_size == "1G"
    assert options.stats is False

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[],
            texts=["25", "10M", "60"],
        ),
        options,
        "site-bounds",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.max_files == 25
    assert options.max_total_size == "10M"
    assert options.max_runtime == 60.0

    options = _apply_customize_overlay(
        FakePrompts(
            selects=[AdaptivePoliteness.fast, ProgressMode.json],
            texts=["8", "2"],
            confirms=[True, True, True, True, True],
        ),
        options,
        "site-adaptive",
    )
    assert isinstance(options, SiteDownloadOptions)
    assert options.adaptive is True
    assert options.max_concurrency == 8
    assert options.per_host_concurrency == 2
    assert options.politeness == AdaptivePoliteness.fast
    assert options.explain is True
    assert options.quiet is True
    assert options.json_output is True
    assert options.progress_mode == ProgressMode.json
    assert options.verbose is True


def test_menu_option_builders_use_settings_defaults(settings: AtlasSettings) -> None:
    video_options = build_video_options(settings, "https://example.com/v")
    assert video_options.archive is True
    assert video_options.use_aria2 is False
    assert video_options.download_engine == DownloadEngineChoice.native

    audio_options = build_audio_options(settings, "https://example.com/a")
    assert audio_options.codec == settings.audio_codec
    assert audio_options.use_aria2 is False
    assert audio_options.download_engine == DownloadEngineChoice.native

    file_options = build_file_options(settings, "https://example.com/a.zip")
    assert file_options.backend == settings.file_backend
    assert build_site_options(settings, "https://example.com/docs").backend == settings.site_backend

    directory_options = build_directory_options(settings, "https://example.com/files/")
    assert directory_options.backend == settings.dir_backend
    assert directory_options.timestamping is True
    assert directory_options.if_modified_since is False
    assert directory_options.user_agent == settings.dir_user_agent


def test_menu_batch_files_have_bounded_retention(tmp_path: Path) -> None:
    newest: Path | None = None
    for index in range(25):
        newest = _write_menu_batch_file(
            tmp_path,
            [f"https://example.com/{index}.zip"],
        )

    assert newest is not None
    assert newest.exists()
    menu_files = list((tmp_path / ".atlas" / "menu").glob("pasted-urls-*.txt"))
    assert len(menu_files) == 20
