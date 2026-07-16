from __future__ import annotations

import json
from io import StringIO
from time import monotonic, sleep

import pytest
from rich.console import Console
from rich.text import Text
from rich.theme import Theme

from atlas.batch import BatchControl, BatchOperatorController
from atlas.models import BatchEntry, EngineKind, HubKind, ProgressEvent, ProgressMode, ProgressPhase
from atlas.progress import (
    BatchProgressReporter,
    FileProgressReporter,
    RichProgressReporter,
    WorkPanelContext,
    _bar_text,
    _batch_stats,
    _event_matches_operator_panel,
    _live_refresh_due,
    _make_progress,
    _next_phase_label,
    _normalize_operator_key,
    _phase_label,
    _phase_state_row,
    _phase_style_for_event,
    _phase_timeline,
    _pulse_bar,
    _pulse_bar_text,
    _render_batch_operator_hint,
    _semantic_event_row,
    _smoothed_progress_event,
    _status_label,
    create_batch_progress_hook,
    create_postprocessor_hook,
    create_progress_hook,
    progress_event_from_ytdlp,
    progress_event_from_ytdlp_postprocessor,
    resolve_progress_mode,
    should_use_alternate_screen,
)
from atlas.progress_events import progress_event_from_aria2_line, progress_event_from_wget2_line
from atlas.theme import (
    ATLAS_ERROR_STYLE,
    ATLAS_MUTED_STYLE,
    ATLAS_PROGRESS_ACTIVE_STYLE,
    ATLAS_PROGRESS_COMPLETE_STYLE,
    ATLAS_PROGRESS_SHIMMER_STYLE,
    ATLAS_PROGRESS_WAITING_STYLE,
    ATLAS_SUCCESS_STYLE,
    ATLAS_WARNING_STYLE,
    AtlasThemeName,
    configure_visuals,
    ensure_atlas_theme,
    reset_visuals,
    resolve_theme,
    status_glyph,
)


class EventCollector:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def hook(self, event: ProgressEvent) -> None:
        self.events.append(event)


def _render_console(output: StringIO, *, width: int | None = None) -> Console:
    return ensure_atlas_theme(
        Console(file=output, force_terminal=True, color_system=None, width=width, height=25)
    )


def test_smoothed_progress_event_blends_speed_and_eta() -> None:
    previous = ProgressEvent(
        engine=EngineKind.aria2,
        status="downloading",
        speed_bytes_per_sec=100.0,
        eta_seconds=100.0,
    )
    current = ProgressEvent(
        engine=EngineKind.aria2,
        status="downloading",
        speed_bytes_per_sec=300.0,
        eta_seconds=40.0,
    )

    smoothed = _smoothed_progress_event(current, previous)

    assert smoothed.speed_bytes_per_sec == 170.0
    assert smoothed.eta_seconds == 79.0


def test_smoothed_progress_event_does_not_cross_item_or_phase_boundaries() -> None:
    previous = ProgressEvent(
        engine=EngineKind.aria2,
        status="downloading",
        phase=ProgressPhase.download,
        item_id="one",
        speed_bytes_per_sec=100.0,
        eta_seconds=100.0,
    )
    next_item = ProgressEvent(
        engine=EngineKind.aria2,
        status="downloading",
        phase=ProgressPhase.download,
        item_id="two",
        speed_bytes_per_sec=300.0,
        eta_seconds=40.0,
    )
    next_phase = next_item.model_copy(update={"item_id": "one", "phase": ProgressPhase.verify})

    assert _smoothed_progress_event(next_item, previous) == next_item
    assert _smoothed_progress_event(next_phase, previous) == next_phase


def test_progress_reporter_history_is_bounded_and_keeps_phase_transitions() -> None:
    reporter = RichProgressReporter(Console(file=StringIO()), mode=ProgressMode.compact)
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.native,
            status="running",
            phase=ProgressPhase.probe,
        )
    )

    for downloaded in range(10_000):
        reporter.hook(
            ProgressEvent(
                engine=EngineKind.native,
                status="downloading",
                phase=ProgressPhase.download,
                downloaded_bytes=downloaded,
                total_bytes=10_000,
            )
        )

    assert len(reporter._events) <= 256
    assert {event.phase for event in reporter._events} == {
        ProgressPhase.probe,
        ProgressPhase.download,
    }
    assert reporter._events[-1].downloaded_bytes == 9_999


def test_canceled_batch_event_is_warning_and_not_counted_as_queued() -> None:
    event = ProgressEvent(
        engine=EngineKind.native,
        status="canceled",
        phase=ProgressPhase.done,
        line_no=1,
    )

    assert _phase_style_for_event(event) == ATLAS_WARNING_STYLE
    assert _batch_stats([event], total=1) == {
        "total": 1,
        "done": 0,
        "failed": 0,
        "active": 0,
        "queued": 0,
        "skipped": 0,
        "canceled": 1,
    }


def test_batch_operator_hint_uses_ascii_keys_without_unicode() -> None:
    try:
        configure_visuals(unicode=False, env={})
        rendered = _render_batch_operator_hint().plain

        assert "up/down move" in rendered
        assert "↑" not in rendered
        assert "↓" not in rendered
    finally:
        reset_visuals()


def test_json_progress_is_one_unstyled_unwrapped_line_on_tty() -> None:
    output = StringIO()
    terminal = Console(
        file=output,
        force_terminal=True,
        color_system="standard",
        width=12,
    )
    reporter = RichProgressReporter(terminal, mode=ProgressMode.json, kind=HubKind.file)
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.native,
            status="downloading",
            filename="long-file-name.bin",
            message="literal [markup] text that must not wrap",
        )
    )

    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert "\x1b[" not in lines[0]
    assert json.loads(lines[0])["message"] == "literal [markup] text that must not wrap"


def test_json_progress_redacts_nested_secrets_and_signed_urls() -> None:
    output = StringIO()
    reporter = RichProgressReporter(
        Console(file=output, force_terminal=True),
        mode=ProgressMode.json,
        kind=HubKind.file,
    )
    secret = "TOPSECRET"

    reporter.hook(
        ProgressEvent(
            engine=EngineKind.native,
            status="error",
            url=f"https://example.com/file?token={secret}",
            message=f"Authorization: Bearer {secret}",
            backend_files=[{"cookie": secret, "uri": f"https://example.com/?sig={secret}"}],
        )
    )

    rendered = output.getvalue()
    payload = json.loads(rendered)
    assert secret not in rendered
    assert payload["backend_files"][0]["cookie"] == "<redacted>"
    assert "token=<redacted>" in payload["url"]


def test_live_refresh_policy_caps_render_frequency() -> None:
    assert not _live_refresh_due(now=10.10, last_rendered_at=10.00)
    assert _live_refresh_due(now=10.25, last_rendered_at=10.00)
    assert _live_refresh_due(now=10.50, last_rendered_at=10.00)


def test_operator_key_normalization_maps_terminal_keys() -> None:
    assert _normalize_operator_key("\t") == "tab"
    assert _normalize_operator_key("\r") == "enter"
    assert _normalize_operator_key("\n") == "enter"
    assert _normalize_operator_key(" ") == "space"
    assert _normalize_operator_key("\x1b[A") == "up"
    assert _normalize_operator_key("\x1b[B") == "down"
    assert _normalize_operator_key("\x1bOA") == "up"
    assert _normalize_operator_key("\x1bOB") == "down"
    assert _normalize_operator_key("?") == "?"


def test_alternate_screen_policy_requires_human_interactive_mode() -> None:
    terminal = Console(file=StringIO(), force_terminal=True)
    pipe = Console(file=StringIO(), force_terminal=False)

    assert should_use_alternate_screen(
        ProgressMode.compact,
        console=terminal,
        plain=False,
    )
    assert should_use_alternate_screen(ProgressMode.full, console=terminal, plain=False)
    assert not should_use_alternate_screen(ProgressMode.json, console=terminal, plain=False)
    assert not should_use_alternate_screen(ProgressMode.none, console=terminal, plain=False)
    assert not should_use_alternate_screen(ProgressMode.compact, console=terminal, plain=True)
    assert not should_use_alternate_screen(ProgressMode.compact, console=pipe, plain=False)


@pytest.mark.parametrize("mode", list(ProgressMode))
def test_json_output_always_suppresses_progress(mode: ProgressMode) -> None:
    terminal = Console(file=StringIO(), force_terminal=True)

    assert (
        resolve_progress_mode(
            mode,
            console=terminal,
            quiet=False,
            json_output=True,
        )
        == ProgressMode.none
    )


def test_live_reporter_passes_alternate_screen_to_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    screens: list[bool] = []

    class FakeLive:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            screens.append(bool(kwargs.get("screen")))

        def __enter__(self) -> FakeLive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def update(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr("atlas.progress.Live", FakeLive)
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        mode=ProgressMode.compact,
        alternate_screen=True,
    )

    with reporter:
        pass

    assert screens == [True]


def test_live_reporters_disable_background_refresh_without_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auto_refresh_values: list[bool] = []

    class FakeLive:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            auto_refresh_values.append(bool(kwargs.get("auto_refresh")))

        def __enter__(self) -> FakeLive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def update(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr("atlas.progress.Live", FakeLive)
    configure_visuals(motion=False, env={})
    try:
        reporters = (
            RichProgressReporter(
                Console(file=StringIO(), force_terminal=True),
                work_context=WorkPanelContext(),
            ),
            FileProgressReporter(
                Console(file=StringIO(), force_terminal=True),
                work_context=WorkPanelContext(),
            ),
            BatchProgressReporter(
                Console(file=StringIO(), force_terminal=True),
                work_context=WorkPanelContext(),
            ),
        )
        for reporter in reporters:
            with reporter:
                pass
    finally:
        reset_visuals()

    assert auto_refresh_values == [False, False, False]


def test_semantic_bar_text_uses_color_spans_and_active_shimmer() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )

        bar = _bar_text(50, width=10, style="bold magenta")

        assert len(bar.plain) == 10
        assert any(char in bar.plain for char in {"▓", "▒", "▌"})
        assert any(str(span.style) == "bold magenta" for span in bar.spans)
        assert any(str(span.style) == ATLAS_PROGRESS_SHIMMER_STYLE for span in bar.spans)
    finally:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_semantic_bar_text_uses_ascii_in_plain_mode() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=True,
            unicode=False,
            color=False,
            env={},
        )

        bar = _bar_text(50, width=10, style="bold magenta")

        assert len(bar.plain) == 10
        assert "#" in bar.plain
        assert "-" in bar.plain
        assert "█" not in bar.plain
        assert "░" not in bar.plain
    finally:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            env={},
        )


def test_semantic_bar_text_respects_disabled_motion() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )

        bar = _bar_text(50, width=10, style="bold magenta")
        pulse = _pulse_bar(width=10)

        assert bar.plain == "█████░░░░░"
        assert all(str(span.style) != ATLAS_PROGRESS_SHIMMER_STYLE for span in bar.spans)
        assert pulse == "▓▓▓▓▓▓▒▒▒▒"
    finally:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_pulse_bar_text_separates_active_and_waiting_styles() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )

        pulse = _pulse_bar_text(width=10, style=ATLAS_WARNING_STYLE)

        assert pulse.plain == "▓▓▓▓▓▓▒▒▒▒"
        assert any(str(span.style) == ATLAS_WARNING_STYLE for span in pulse.spans)
        assert any(str(span.style) == ATLAS_PROGRESS_WAITING_STYLE for span in pulse.spans)
    finally:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_semantic_progress_rows_render_retry_and_backoff_as_warning() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )
        retry = ProgressEvent(
            engine=EngineKind.native,
            status="retrying",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            retry_count=1,
        )
        backoff = ProgressEvent(
            engine=EngineKind.native,
            status="backoff",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            message="host cooling down",
        )

        retry_row = _semantic_event_row("Download", retry)
        backoff_row = _phase_state_row("Download", backoff)

        assert "retrying" in _status_label(retry)
        assert "backoff" in _status_label(backoff)
        assert retry_row.plain.startswith("Download")
        assert "retrying" in retry_row.plain
        assert "host cooling down" in backoff_row.plain
        assert any(str(span.style) == ATLAS_WARNING_STYLE for span in retry_row.spans)
        assert any(str(span.style) == ATLAS_WARNING_STYLE for span in backoff_row.spans)
    finally:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_phase_labels_use_semantic_state_styles() -> None:
    done = ProgressEvent(
        engine=EngineKind.native,
        status="done",
        phase=ProgressPhase.done,
        kind=HubKind.file,
    )
    failed = ProgressEvent(
        engine=EngineKind.native,
        status="failed",
        phase=ProgressPhase.error,
        kind=HubKind.file,
    )
    backoff = ProgressEvent(
        engine=EngineKind.native,
        status="backoff",
        phase=ProgressPhase.download,
        kind=HubKind.file,
    )

    assert _phase_label(done, full=False) == f"[{ATLAS_SUCCESS_STYLE}]Done[/{ATLAS_SUCCESS_STYLE}]"
    assert _phase_label(failed, full=False) == f"[{ATLAS_ERROR_STYLE}]Error[/{ATLAS_ERROR_STYLE}]"
    assert _phase_label(backoff, full=False) == (
        f"[{ATLAS_WARNING_STYLE}]Downloading[/{ATLAS_WARNING_STYLE}]"
    )
    assert _phase_label(done, full=True).startswith(f"[{ATLAS_SUCCESS_STYLE}]Done")
    assert _phase_label(failed, full=True).startswith(f"[{ATLAS_ERROR_STYLE}]Error")


def test_phase_timeline_uses_semantic_text_spans() -> None:
    timeline = _phase_timeline(
        [
            ProgressEvent(
                engine=EngineKind.native,
                status="done",
                phase=ProgressPhase.probe,
                kind=HubKind.file,
            ),
            ProgressEvent(
                engine=EngineKind.native,
                status="downloading",
                phase=ProgressPhase.download,
                kind=HubKind.file,
            ),
        ]
    )

    assert timeline.plain.startswith("✓ Probing > ○ Extracting > → Downloading")
    styles = {str(span.style) for span in timeline.spans}
    assert ATLAS_PROGRESS_COMPLETE_STYLE in styles
    assert ATLAS_PROGRESS_ACTIVE_STYLE in styles
    assert ATLAS_PROGRESS_WAITING_STYLE in styles
    assert ATLAS_MUTED_STYLE in styles


def test_progress_error_notice_uses_semantic_text_object() -> None:
    reporter = RichProgressReporter(Console(file=StringIO()), mode=ProgressMode.compact)
    printed: list[object] = []
    reporter.console.print = lambda obj, *args, **kwargs: printed.append(obj)  # type: ignore[method-assign]

    reporter.hook(
        ProgressEvent(
            engine=EngineKind.native,
            status="failed",
            phase=ProgressPhase.error,
            kind=HubKind.file,
            title="archive.zip",
        )
    )

    assert len(printed) == 1
    notice = printed[0]
    assert isinstance(notice, Text)
    assert notice.plain == "Error archive.zip"
    assert any(str(span.style) == ATLAS_ERROR_STYLE for span in notice.spans)


def test_progress_spinner_and_activity_frame_respect_disabled_motion() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )

        progress = _make_progress(Console(file=StringIO(), force_terminal=True), label="")
        label = _status_label(
            ProgressEvent(
                engine=EngineKind.native,
                status="downloading",
                phase=ProgressPhase.download,
            )
        )

        assert all(column.__class__.__name__ != "SpinnerColumn" for column in progress.columns)
        assert "- running" in label
    finally:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )


def test_progress_event_from_ytdlp_maps_core_fields() -> None:
    event = progress_event_from_ytdlp(
        {
            "status": "downloading",
            "filename": "/tmp/example.webm",
            "downloaded_bytes": 512,
            "total_bytes_estimate": 1024,
            "speed": 256.0,
            "eta": 2,
            "info_dict": {
                "id": "abc123",
                "title": "Example",
                "webpage_url": "https://example.com/watch?v=abc123",
            },
        }
    )

    assert event.engine == EngineKind.ytdlp
    assert event.status == "downloading"
    assert event.filename == "/tmp/example.webm"
    assert event.title == "Example"
    assert event.item_id == "abc123"
    assert event.downloaded_bytes == 512
    assert event.total_bytes == 1024
    assert event.speed_bytes_per_sec == 256.0
    assert event.eta_seconds == 2.0


def test_next_phase_label_is_kind_specific() -> None:
    file_next = _next_phase_label(
        [
            ProgressEvent(
                engine=EngineKind.native,
                kind=HubKind.file,
                phase=ProgressPhase.download,
                status="downloading",
            )
        ]
    )
    media_next = _next_phase_label(
        [
            ProgressEvent(
                engine=EngineKind.ytdlp,
                kind=HubKind.video,
                phase=ProgressPhase.download,
                status="downloading",
            )
        ]
    )
    audio_next = _next_phase_label(
        [
            ProgressEvent(
                engine=EngineKind.ytdlp,
                kind=HubKind.audio,
                phase=ProgressPhase.download,
                status="downloading",
            )
        ]
    )

    assert file_next == "Verify · Finalize"
    assert "Merge video/audio" in media_next
    assert "Extract audio" not in media_next
    assert "Download audio" in audio_next
    assert "Merge video/audio" not in audio_next
    assert "Embed metadata" in audio_next


def test_progress_event_from_ytdlp_maps_fragment_progress() -> None:
    event = progress_event_from_ytdlp(
        {
            "status": "downloading",
            "fragment_index": 3,
            "fragment_count": 12,
            "eta": 9,
            "info_dict": {"title": "Fragmented Stream"},
        }
    )

    assert event.title == "Fragmented Stream"
    assert event.fragment_index == 3
    assert event.fragment_count == 12
    assert event.eta_seconds == 9.0


def test_create_progress_hook_converts_ytdlp_payload() -> None:
    collector = EventCollector()
    hook = create_progress_hook(collector)  # type: ignore[arg-type]

    hook({"status": "finished", "filename": "/tmp/example.mkv"})

    assert collector.events == [
        ProgressEvent(engine=EngineKind.ytdlp, status="finished", filename="/tmp/example.mkv")
    ]


def test_progress_event_from_ytdlp_postprocessor_maps_phase() -> None:
    event = progress_event_from_ytdlp_postprocessor(
        {
            "status": "started",
            "postprocessor": "FFmpegMerger",
            "info_dict": {
                "id": "abc123",
                "title": "Example",
                "filepath": "/tmp/example.mkv",
                "webpage_url": "https://example.com/watch?v=abc123",
            },
        },
        kind=HubKind.video,
    )

    assert event.engine == EngineKind.ytdlp
    assert event.status == "running"
    assert event.phase == ProgressPhase.merge
    assert event.kind == HubKind.video
    assert event.filename == "/tmp/example.mkv"
    assert event.message == "FFmpegMerger running"


def test_create_postprocessor_hook_converts_ytdlp_payload() -> None:
    collector = EventCollector()
    hook = create_postprocessor_hook(collector, kind=HubKind.audio)  # type: ignore[arg-type]

    hook({"status": "finished", "postprocessor": "FFmpegExtractAudio"})

    assert collector.events == [
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="done",
            phase=ProgressPhase.extract,
            kind=HubKind.audio,
            message="FFmpegExtractAudio finished",
        )
    ]


def test_postprocessor_errors_preserve_semantic_phase() -> None:
    event = progress_event_from_ytdlp_postprocessor(
        {"status": "error", "postprocessor": "FFmpegExtractAudio"},
        kind=HubKind.audio,
    )

    assert event.status == "error"
    assert event.phase == ProgressPhase.extract
    assert event.message == "FFmpegExtractAudio error"


def test_single_media_stack_shows_separate_postprocess_rows() -> None:
    reporter = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(kind=HubKind.audio),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="finished",
            phase=ProgressPhase.download,
            kind=HubKind.audio,
            title="Example",
            downloaded_bytes=100,
            total_bytes=100,
        )
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="running",
            phase=ProgressPhase.extract,
            kind=HubKind.audio,
            title="Example",
            message="FFmpegExtractAudio running",
        )
    )

    output = StringIO()
    _render_console(output).print(reporter._render())
    rendered = output.getvalue()

    assert "\u256d\u2500 Downloading" in rendered
    assert "Download audio" in rendered
    assert "Merge video/audio" not in rendered
    assert "FFmpegExtractAudio" not in rendered
    assert "Embed metadata" in rendered
    assert "Add artwork" in rendered
    assert "Finalize" in rendered


def test_video_progress_uses_download_card_with_compact_steps() -> None:
    reporter = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(
            kind=HubKind.video,
            item_title="How to Get a New Identity and Disappear.",
            source="YouTube · Into the Shadows",
            quality="1080p · H.264 · MP4",
            output="~/Downloads/atlas",
            steps=(
                "Download video",
                "Merge video/audio",
                "Embed metadata",
                "Add thumbnail",
                "Finalize",
            ),
        ),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.video,
            title="How to Get a New Identity and Disappear.",
            downloaded_bytes=107_000_000,
            total_bytes=126_800_000,
            speed_bytes_per_sec=9_100_000,
            eta_seconds=1,
        )
    )

    output = StringIO()
    _render_console(output, width=110).print(reporter._render())
    rendered = output.getvalue()

    assert "\u256d\u2500 Downloading" in rendered
    assert "Title" in rendered
    assert "How to Get a New Identity and Disappear." in rendered
    assert "Source" in rendered
    assert "YouTube · Into the Shadows" in rendered
    assert "Quality" in rendered
    assert "1080p · H.264 · MP4" in rendered
    assert "Download" in rendered
    assert "84%" in rendered
    assert "107.0 MB / 126.8 MB" in rendered
    assert "Speed" in rendered
    assert "ETA 00:01" in rendered
    assert "\u25b8 Download video" in rendered
    assert "\u25cb Merge video/audio" in rendered
    assert "q cancel" not in rendered


def test_media_progress_renders_exact_plan_steps_including_empty_plan() -> None:
    reporter = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(
            kind=HubKind.video,
            item_title="Single stream",
            steps=("Download video", "Finalize"),
        ),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.video,
            title="Single stream",
            downloaded_bytes=1,
            total_bytes=2,
        )
    )

    output = StringIO()
    _render_console(output, width=80).print(reporter._render())
    rendered = output.getvalue()

    assert "Download video" in rendered
    assert "Finalize" in rendered
    assert "Merge video/audio" not in rendered
    assert "Embed metadata" not in rendered
    assert "Add thumbnail" not in rendered

    no_steps = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(kind=HubKind.video, steps=()),
    )
    no_steps_output = StringIO()
    _render_console(no_steps_output, width=80).print(no_steps._render())
    assert "Steps" not in no_steps_output.getvalue()


def test_file_progress_only_shows_transfer_phases_that_apply() -> None:
    reporter = FileProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(kind=HubKind.file),
    )
    reporter.handle_event(
        ProgressEvent(
            engine=EngineKind.native,
            status="done",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            title="README.md",
        )
    )
    reporter.handle_event(
        ProgressEvent(
            engine=EngineKind.native,
            status="running",
            phase=ProgressPhase.verify,
            kind=HubKind.file,
            title="README.md",
        )
    )

    output = StringIO()
    _render_console(output, width=80).print(reporter._render())
    rendered = output.getvalue()

    assert "Download" in rendered
    assert "Verify" in rendered
    assert "Merge" not in rendered
    assert "Extract" not in rendered
    assert "metadata" not in rendered
    assert "Thumbnail" not in rendered


def test_human_file_progress_redacts_signed_urls_and_secret_messages() -> None:
    reporter = FileProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(kind=HubKind.file),
    )
    reporter.handle_event(
        ProgressEvent(
            engine=EngineKind.native,
            status="error",
            phase=ProgressPhase.error,
            kind=HubKind.file,
            url="https://cdn.example/file?token=SECURITY_SENTINEL",
            message="token=SECURITY_SENTINEL",
        )
    )

    output = StringIO()
    _render_console(output, width=80).print(reporter._render())
    rendered = output.getvalue()

    assert "SECURITY_SENTINEL" not in rendered


def test_human_file_progress_strips_terminal_control_sequences() -> None:
    output = StringIO()
    reporter = FileProgressReporter(
        _render_console(output),
        mode=ProgressMode.compact,
        title="fallback",
    )

    with reporter:
        reporter.handle_event(
            ProgressEvent(
                engine=EngineKind.native,
                status="downloading",
                phase=ProgressPhase.download,
                kind=HubKind.file,
                title="evil\x1b[2J\x1b]0;owned\x07name\u202etxt",
                downloaded_bytes=1,
                total_bytes=2,
            )
        )

    rendered = output.getvalue()
    assert "\x1b[2J" not in rendered
    assert "\x1b]0;owned" not in rendered
    assert "\u202e" not in rendered
    assert "evilnametxt" in rendered


def test_no_unicode_progress_uses_only_ascii_spinner_and_bar() -> None:
    output = StringIO()
    configure_visuals(color=False, unicode=False, motion=True, env={})
    progress = _make_progress(
        Console(file=output, force_terminal=True, color_system=None, width=80),
        label="",
    )

    with progress:
        progress.add_task(
            "download",
            phase="Downloading",
            title="file.bin",
            bytes_label="1 / 2 B",
            speed_label="1 B/s",
            eta_label="1s",
            completed=1,
            total=2,
        )
        progress.refresh()

    assert output.getvalue().isascii()


def test_create_batch_progress_hook_preserves_batch_context() -> None:
    reporter = BatchProgressReporter(Console(file=StringIO()))
    events: list[ProgressEvent] = []
    reporter.hook = events.append  # type: ignore[method-assign]
    hook = create_batch_progress_hook(
        reporter,
        line_no=7,
        url="https://example.com/item",
    )

    hook({"status": "downloading", "downloaded_bytes": 10})

    event = events[0]
    assert event.engine == EngineKind.ytdlp
    assert event.line_no == 7
    assert event.item_id == "7"
    assert event.url == "https://example.com/item"
    assert event.downloaded_bytes == 10


def test_progress_event_from_aria2_line_parses_readout() -> None:
    event = progress_event_from_aria2_line(
        "[#2089b0 400KiB/33MiB(1%) CN:1 DL:115KiB ETA:4m53s]",
        filename="file.iso",
        url="https://example.com/file.iso",
    )

    assert event is not None
    assert event.engine == EngineKind.aria2
    assert event.status == "downloading"
    assert event.filename == "file.iso"
    assert event.downloaded_bytes == 400 * 1024
    assert event.total_bytes == 33 * 1024 * 1024
    assert event.speed_bytes_per_sec == 115 * 1024
    assert event.eta_seconds == 293.0


def test_progress_event_from_wget2_line_emits_coarse_event() -> None:
    event = progress_event_from_wget2_line(
        "example.zip 42% [=======>            ]",
        filename="mirror",
        url="https://example.com/",
    )

    assert event is not None
    assert event.engine == EngineKind.wget2
    assert event.status == "downloading"
    assert event.filename == "mirror"
    assert event.percent == 42.0
    assert event.message == "example.zip 42% [=======> ]"


def test_live_work_panel_render_includes_context_and_phases() -> None:
    reporter = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(
            queue_count=1,
            safety_badges=("single video", "aria2"),
        ),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="done",
            phase=ProgressPhase.probe,
            kind=HubKind.video,
            title="metadata",
        )
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.video,
            title="Example",
            downloaded_bytes=512,
            total_bytes=1024,
            speed_bytes_per_sec=256,
            eta_seconds=2,
        )
    )

    output = StringIO()
    _render_console(output).print(reporter._render())
    rendered = output.getvalue()

    assert "Mode" not in rendered
    assert "Backends" not in rendered
    assert "Archive" not in rendered
    assert "Download" in rendered
    assert "Speed" in rendered
    assert "ETA" in rendered
    assert "512 B / 1.0 kB" in rendered
    assert "Elapsed" in rendered
    assert "Safety" in rendered
    assert "single video" in rendered
    assert "Next" in rendered
    assert "Downloading" in rendered
    assert f"{status_glyph('selected')} Phase" in rendered
    assert f"{status_glyph('transition')} Next" in rendered
    assert "done  done" not in rendered


def test_live_work_panel_active_count_uses_latest_event_per_item() -> None:
    reporter = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(
            queue_count=1,
            safety_badges=("single video",),
        ),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.aria2,
            status="running",
            phase=ProgressPhase.download,
            kind=HubKind.video,
            url="https://example.com/watch?v=abc",
            title="external downloader",
        )
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="done",
            phase=ProgressPhase.merge,
            kind=HubKind.video,
            url="https://example.com/watch?v=abc",
            title="Example",
        )
    )

    output = StringIO()
    _render_console(output).print(reporter._render())
    rendered = output.getvalue()

    assert "Merge" in rendered
    assert "Finalize" in rendered
    assert "waiting" in rendered


def test_single_download_startup_event_does_not_render_as_waiting() -> None:
    reporter = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(
            queue_count=1,
            safety_badges=("single video", "aria2"),
        ),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.aria2,
            status="running",
            phase=ProgressPhase.download,
            kind=HubKind.video,
            title="aria2c external downloader",
            message="temporary .part files are normal until merge",
        )
    )

    output = StringIO()
    _render_console(output).print(reporter._render())
    rendered = output.getvalue()

    assert "Download" in rendered
    assert "temporary .part files are normal until merge" in rendered
    assert "waiting" not in rendered


def test_empty_single_download_progress_starts_active() -> None:
    reporter = RichProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        work_context=WorkPanelContext(queue_count=1),
    )

    output = StringIO()
    _render_console(output).print(reporter._render())
    rendered = output.getvalue()

    assert "Download" in rendered
    assert "starting" in rendered
    assert "waiting" not in rendered


def test_batch_render_avoids_status_progress_duplicates() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        concurrency=4,
        mode=ProgressMode.full,
        work_context=WorkPanelContext(
            queue_count=4,
            safety_badges=("concurrency 4", "sites skipped"),
        ),
    )
    for event in [
        ProgressEvent(
            engine=EngineKind.native,
            status="done",
            phase=ProgressPhase.done,
            kind=HubKind.file,
            line_no=1,
            item_id="1",
            title="small.txt",
        ),
        ProgressEvent(
            engine=EngineKind.aria2,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=2,
            item_id="2",
            title="large.iso",
            downloaded_bytes=800_000_000,
            total_bytes=4_700_000_000,
            speed_bytes_per_sec=88_000_000,
            eta_seconds=43,
            selected_backend="aria2",
            retry_count=1,
            queue_concurrency=40,
            per_file_segments=16,
            max_total_connections=96,
            scheduler_decision="large ranged file: low queue concurrency with per-file segments",
        ),
        ProgressEvent(
            engine=EngineKind.ytdlp,
            status="running",
            phase=ProgressPhase.download,
            kind=HubKind.video,
            line_no=3,
            item_id="3",
            title="Example Video",
            fragment_index=8,
            fragment_count=42,
            eta_seconds=160,
        ),
        ProgressEvent(
            engine=EngineKind.wget2,
            status="queued",
            phase=ProgressPhase.extract,
            kind=HubKind.site,
            line_no=4,
            item_id="4",
            title="docs mirror",
        ),
    ]:
        reporter.hook(event)

    output = StringIO()
    _render_console(output, width=180).print(reporter._render())
    rendered = output.getvalue()

    assert "2 jobs" in rendered
    assert "88.0 MB/s" in rendered
    assert "1 retries" in rendered
    assert "16/96 connections" in rendered
    assert "large" in rendered
    assert "ranged file" in rendered
    assert "large.iso" in rendered
    assert "Example Video" in rendered
    assert "done                             done" not in rendered
    assert "queued                         queued" not in rendered
    assert "small.txt" in rendered
    assert "done" in rendered
    assert "docs" in rendered
    assert "mirror" in rendered
    assert "queued" in rendered
    assert "active" in rendered
    assert "Shortcuts" in rendered
    assert "tab panels" in rendered
    assert "g pause all" not in rendered
    assert "x cancel item" not in rendered
    assert "p preview" not in rendered
    assert "q back" not in rendered


def test_batch_reporter_operator_key_cancels_focused_active_item() -> None:
    control = BatchControl()
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        concurrency=2,
        mode=ProgressMode.full,
        work_context=WorkPanelContext(queue_count=2),
        operator_controller=BatchOperatorController(control),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.native,
            status="queued",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=1,
            item_id="1",
            url="https://example.com/one.iso",
            title="one.iso",
        )
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.aria2,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=2,
            item_id="2",
            url="https://downloads.example.com/two.iso",
            title="two.iso",
        )
    )

    result = reporter.handle_operator_key("x")

    assert result is not None
    assert result.action == "cancel_line"
    assert result.snapshot["canceled_lines"] == [2]
    output = StringIO()
    _render_console(output, width=140).print(reporter._render())
    assert "cancel requested for item 2" in output.getvalue()


def test_batch_reporter_help_overlay_toggles_with_question_key() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        concurrency=2,
        mode=ProgressMode.full,
        work_context=WorkPanelContext(queue_count=2),
    )

    result = reporter.handle_operator_key("?")

    assert result is not None
    assert result.action == "toggle_help"
    assert result.snapshot["shortcut_help"] is True
    output = StringIO()
    _render_console(output, width=140).print(reporter._render())
    rendered = output.getvalue()
    assert "Shortcuts" in rendered
    assert "show batch shortcuts" in rendered
    assert "filter menus" not in rendered
    assert "p Preview" not in rendered
    assert "q Back" not in rendered

    result = reporter.handle_operator_key("?")

    assert result is not None
    assert result.snapshot["shortcut_help"] is False
    output = StringIO()
    _render_console(output, width=140).print(reporter._render())
    assert "show batch shortcuts" not in output.getvalue()


def test_batch_reporter_listens_for_view_keys_without_live_controls() -> None:
    class ViewKeySource:
        def __init__(self) -> None:
            self.sent = False
            self.closed = False

        def __call__(self) -> str | None:
            if self.sent:
                return None
            self.sent = True
            return "?"

        def close(self) -> None:
            self.closed = True

    source = ViewKeySource()
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        concurrency=2,
        mode=ProgressMode.full,
        work_context=WorkPanelContext(queue_count=2),
        operator_key_source=source,
    )

    with reporter:
        deadline = monotonic() + 1
        while not reporter._show_shortcut_overlay and monotonic() < deadline:
            sleep(0.01)

    assert reporter._show_shortcut_overlay is True
    assert source.closed is True


def test_batch_reporter_tab_cycles_operator_panels() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        concurrency=2,
        mode=ProgressMode.full,
        work_context=WorkPanelContext(queue_count=2),
    )

    result = reporter.handle_operator_key("\t")

    assert result is not None
    assert result.action == "cycle_panel"
    assert result.snapshot["active_panel"] == "completed"
    output = StringIO()
    _render_console(output, width=140).print(reporter._render())
    assert "[completed]" in output.getvalue()


def test_batch_reporter_operator_panels_filter_visible_rows() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        concurrency=2,
        mode=ProgressMode.full,
        work_context=WorkPanelContext(queue_count=3),
    )
    for line_no, status, title in (
        (1, "queued", "queued.iso"),
        (2, "downloading", "active.iso"),
        (3, "finished", "complete.iso"),
    ):
        reporter.hook(
            ProgressEvent(
                engine=EngineKind.native,
                status=status,
                phase=ProgressPhase.download,
                kind=HubKind.file,
                line_no=line_no,
                item_id=str(line_no),
                title=title,
            )
        )

    reporter.handle_operator_key("tab")
    output = StringIO()
    _render_console(output, width=140).print(reporter._render())

    rendered = output.getvalue()
    assert "[completed]" in rendered
    assert "complete.iso" in rendered
    assert "queued.iso" not in rendered
    assert "active.iso" not in rendered


def test_batch_reporter_arrow_focus_controls_cancel_target() -> None:
    control = BatchControl()
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        concurrency=2,
        mode=ProgressMode.full,
        work_context=WorkPanelContext(queue_count=3),
        operator_controller=BatchOperatorController(control),
    )
    for line_no, status in ((1, "queued"), (2, "downloading"), (3, "downloading")):
        reporter.hook(
            ProgressEvent(
                engine=EngineKind.native,
                status=status,
                phase=ProgressPhase.download,
                kind=HubKind.file,
                line_no=line_no,
                item_id=str(line_no),
                url=f"https://example.com/{line_no}.txt",
                title=f"{line_no}.txt",
            )
        )

    focus_result = reporter.handle_operator_key("\x1b[B")
    cancel_result = reporter.handle_operator_key("x")

    assert focus_result is not None
    assert focus_result.action == "move_focus"
    assert focus_result.snapshot["focused_line"] == 3
    assert cancel_result is not None
    assert cancel_result.snapshot["canceled_lines"] == [3]
    output = StringIO()
    _render_console(output, width=140).print(reporter._render())
    assert ">3" in output.getvalue()


def test_batch_reporter_full_mode_reads_injected_operator_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLive:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def __enter__(self) -> FakeLive:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def update(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr("atlas.progress.Live", FakeLive)
    control = BatchControl()
    keys = iter(["g"])

    def key_source() -> str | None:
        return next(keys, None)

    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        mode=ProgressMode.full,
        work_context=WorkPanelContext(queue_count=1),
        operator_controller=BatchOperatorController(control),
        operator_key_source=key_source,
    )

    with reporter:
        deadline = monotonic() + 1
        while not control.snapshot()["global_paused"] and monotonic() < deadline:
            sleep(0.01)

    assert control.snapshot()["global_paused"] is True


def test_batch_render_uses_compact_rows_on_narrow_terminals() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), width=64),
        total=3,
        concurrency=2,
        mode=ProgressMode.compact,
        work_context=WorkPanelContext(
            operation="Batch Download",
            safety_badges=("archive on", "dirs bounded"),
        ),
    )
    for event in [
        ProgressEvent(
            engine=EngineKind.native,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=18,
            item_id="18",
            title="manual-1987-with-a-very-long-name.pdf",
            downloaded_bytes=7_300_000,
            total_bytes=8_000_000,
            speed_bytes_per_sec=3_100_000,
            eta_seconds=2,
        ),
        ProgressEvent(
            engine=EngineKind.native,
            status="retrying",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=93,
            item_id="93",
            title="small-cover.jpg",
            retry_count=1,
        ),
    ]:
        reporter.hook(event)

    output = StringIO()
    _render_console(output, width=64).print(reporter._render())
    rendered = output.getvalue()

    assert "Kind" not in rendered
    assert "manual-1987" in rendered
    assert "00:02" in rendered
    assert "retry 1" in rendered
    assert "Shortcuts" not in rendered
    assert "\n                             P" not in rendered


def test_batch_render_limits_rows_to_terminal_height_and_prioritizes_actionable_items() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), width=100, height=18),
        total=30,
        concurrency=3,
        mode=ProgressMode.compact,
        work_context=WorkPanelContext(operation="Batch Download"),
    )
    for line_no in range(1, 28):
        reporter.hook(
            ProgressEvent(
                engine=EngineKind.native,
                status="queued" if line_no < 20 else "done",
                phase=ProgressPhase.download if line_no < 20 else ProgressPhase.done,
                kind=HubKind.file,
                line_no=line_no,
                item_id=str(line_no),
                title=f"ordinary-{line_no}.bin",
            )
        )
    for line_no, status, title in (
        (28, "failed", "failed-priority.bin"),
        (29, "retrying", "retry-priority.bin"),
        (30, "downloading", "active-priority.bin"),
    ):
        reporter.hook(
            ProgressEvent(
                engine=EngineKind.native,
                status=status,
                phase=ProgressPhase.error if status == "failed" else ProgressPhase.download,
                kind=HubKind.file,
                line_no=line_no,
                item_id=str(line_no),
                title=title,
            )
        )

    output = StringIO()
    _render_console(output, width=100).print(reporter._render())
    rendered = output.getvalue()

    assert "active-priority.bin" in rendered
    assert "retry-priority.bin" in rendered
    assert "failed-priority.bin" in rendered
    assert "hidden" in rendered
    assert "ordinary-1.bin" not in rendered


def test_compact_batch_rows_keep_speed_and_eta_together_at_40_columns() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), width=40),
        total=1,
        mode=ProgressMode.compact,
        work_context=WorkPanelContext(operation="Batch Download"),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.native,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=18,
            item_id="18",
            title="manual-1987-with-a-very-long-name.pdf",
            downloaded_bytes=7_300_000,
            total_bytes=8_000_000,
            speed_bytes_per_sec=3_100_000,
            eta_seconds=2,
        )
    )

    output = StringIO()
    _render_console(output, width=40).print(reporter._render())
    rendered = output.getvalue()

    assert "3.1 MB/s" in rendered
    assert "00:02" in rendered


def test_batch_reporter_seeds_queue_and_renders_percent_progress() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        total=3,
        concurrency=2,
        work_context=WorkPanelContext(
            queue_count=3,
            safety_badges=("concurrency 2", "adaptive normal"),
        ),
    )
    reporter.seed_entries(
        [
            BatchEntry(line_no=1, url="https://example.com/one.iso"),
            BatchEntry(line_no=2, url="https://example.com/two.iso"),
            BatchEntry(line_no=3, url="https://example.com/three.iso"),
        ],
        kind=HubKind.file,
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.aria2,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=2,
            item_id="2",
            title="two.iso",
            downloaded_bytes=50,
            total_bytes=100,
            speed_bytes_per_sec=25,
            eta_seconds=2,
        )
    )

    output = StringIO()
    _render_console(output, width=140).print(reporter._render())
    rendered = output.getvalue()

    assert "one.iso" in rendered
    assert "two.iso" in rendered
    assert "three.iso" in rendered
    assert "50%" in rendered
    assert "50 B / 100 B" in rendered
    assert "░░░░░" in rendered
    assert any(char in rendered for char in {"▓", "▒", "▌"})
    assert "queued" in rendered
    assert "concurrency 2" in rendered
    assert "Speed" in rendered
    assert "25 B/s total" in rendered


def test_batch_reporter_renders_wget2_percent_progress() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        total=1,
        concurrency=1,
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.wget2,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=1,
            item_id="1",
            title="archive.zip",
            percent=42.0,
        )
    )

    output = StringIO()
    _render_console(output, width=120).print(reporter._render())
    rendered = output.getvalue()

    assert " 42%" in rendered
    assert "░░░░░░" in rendered
    assert any(char in rendered for char in {"▓", "▒", "▌"})


def test_batch_reporter_full_mode_surfaces_scheduler_note_inside_progress_panel() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True, width=140),
        total=1,
        concurrency=4,
        mode=ProgressMode.full,
        layout_width=140,
        work_context=WorkPanelContext(
            queue_count=1,
            safety_badges=("adaptive",),
        ),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.wget2,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.dir,
            line_no=1,
            item_id="1",
            title="public/",
            downloaded_bytes=42,
            total_bytes=100,
            speed_bytes_per_sec=21,
            eta_seconds=2,
            queue_concurrency=4,
            max_total_connections=16,
            per_host_concurrency=6,
            scheduler_decision="small-file lane increased 16 -> 24",
        )
    )

    output = StringIO()
    _render_console(output, width=140).print(reporter._render())
    rendered = output.getvalue()

    assert "Progress" in rendered
    assert "Scheduler" in rendered
    assert "small-file lane increased 16 -> 24" in rendered
    assert "Progress        0 done" not in rendered


def test_batch_reporter_full_mode_renders_wide_table_mini_bars() -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True),
        total=2,
        concurrency=2,
        mode=ProgressMode.full,
        layout_width=160,
        work_context=WorkPanelContext(queue_count=2),
    )
    for event in [
        ProgressEvent(
            engine=EngineKind.aria2,
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=42,
            item_id="42",
            title="dataset.tar.zst",
            downloaded_bytes=7_900_000_000,
            total_bytes=18_000_000_000,
            speed_bytes_per_sec=62_000_000,
            eta_seconds=192,
            selected_backend="aria2",
        ),
        ProgressEvent(
            engine=EngineKind.native,
            status="retrying",
            phase=ProgressPhase.download,
            kind=HubKind.file,
            line_no=93,
            item_id="93",
            title="small-cover.jpg",
            retry_count=1,
        ),
    ]:
        reporter.hook(event)

    output = StringIO()
    Console(
        file=output,
        width=160,
        force_terminal=False,
        theme=Theme(resolve_theme(AtlasThemeName.auto)),
    ).print(reporter._render())
    rendered = output.getvalue()

    assert "dataset.tar.zst" in rendered
    assert "Xfer" in rendered
    assert "7.9/18 GB" in rendered
    assert " 43%" in rendered
    assert any(char in rendered for char in {"▓", "▒", "▌"})
    assert "Eng" in rendered
    assert "aria2" in rendered


@pytest.mark.parametrize(
    ("width", "full_layout"),
    [(109, False), (110, True), (120, True), (140, True)],
)
def test_batch_table_switches_without_wrapping_fixed_cells(
    width: int,
    full_layout: bool,
) -> None:
    reporter = BatchProgressReporter(
        Console(file=StringIO(), force_terminal=True, color_system=None, width=width, height=25),
        total=1,
        mode=ProgressMode.full,
        layout_width=width,
        work_context=WorkPanelContext(queue_count=1),
    )
    reporter.hook(
        ProgressEvent(
            engine=EngineKind.native,
            selected_backend="unknown",
            status="downloading",
            phase=ProgressPhase.download,
            kind=HubKind.manifest,
            line_no=1,
            item_id="1",
            title="manifest-with-a-long-but-readable-name.meta4",
            downloaded_bytes=123_500_000_000,
            total_bytes=999_900_000_000,
            speed_bytes_per_sec=123_500_000,
            eta_seconds=12_345,
        )
    )

    output = StringIO()
    console = _render_console(output, width=width)
    assert console.width == width
    console.print(reporter._render())
    rendered = output.getvalue()

    if not full_layout:
        assert "Kind" not in rendered
        return

    data_row = next(line for line in rendered.splitlines() if "manifest" in line)
    assert "123.5 MB/s" in data_row
    assert "3:25:45" in data_row
    assert "unknown" in data_row
    assert len(data_row) <= width


def test_retrying_batch_event_is_active_not_failed_panel() -> None:
    retrying = ProgressEvent(
        engine=EngineKind.curl,
        status="retrying",
        phase=ProgressPhase.download,
        kind=HubKind.file,
        line_no=1,
        item_id="1",
        title="book.epub",
    )
    backoff = retrying.model_copy(update={"status": "backoff"})
    failed = retrying.model_copy(
        update={
            "status": "error",
            "phase": ProgressPhase.error,
            "message": "curl exited 60",
        }
    )

    assert _event_matches_operator_panel(retrying, "active")
    assert _event_matches_operator_panel(backoff, "active")
    assert not _event_matches_operator_panel(retrying, "failed")
    assert not _event_matches_operator_panel(backoff, "failed")
    assert _event_matches_operator_panel(failed, "failed")
