"""Rich progress integration for yt-dlp hooks."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Event, RLock, Thread
from time import monotonic
from types import TracebackType
from typing import Any, TextIO
from urllib.parse import unquote, urlparse

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
)
from rich.table import Table
from rich.text import Text

from atlas.batch import BatchOperatorController, BatchOperatorResult
from atlas.models import BatchEntry, EngineKind, HubKind, ProgressEvent, ProgressMode, ProgressPhase
from atlas.progress_events import progress_event_from_ytdlp, progress_event_from_ytdlp_postprocessor
from atlas.theme import (
    ATLAS_ACTIVE_STYLE,
    ATLAS_ERROR_STYLE,
    ATLAS_MUTED_STYLE,
    ATLAS_PANEL_STYLE,
    ATLAS_PROGRESS_ACTIVE_STYLE,
    ATLAS_PROGRESS_COMPLETE_STYLE,
    ATLAS_PROGRESS_FILE_STYLE,
    ATLAS_PROGRESS_MEDIA_STYLE,
    ATLAS_PROGRESS_MIRROR_STYLE,
    ATLAS_PROGRESS_WAITING_STYLE,
    ATLAS_SUCCESS_STYLE,
    ATLAS_TITLE_STYLE,
    ATLAS_WARNING_STYLE,
    atlas_box,
    ensure_atlas_theme,
    semantic_bar_text,
    semantic_pulse_bar_text,
    status_glyph,
    themed_console,
    visual_join,
    visual_options,
    visual_separator,
)
from atlas.views import OperatorAction, OperatorKeymap, SmartSessionView, ViewField

ProgressHook = Callable[[dict[str, Any]], None]
ProgressEventHandler = Callable[[ProgressEvent], None]
OperatorKeySource = Callable[[], str | None]

_PROGRESS_SMOOTHING_ALPHA = 0.35
_ACTIVITY_FRAMES = ("|", "/", "-", "\\")
_SEMANTIC_BAR_WIDTH = 20
_FULL_BATCH_TABLE_MIN_WIDTH = 110
_LIVE_REFRESH_PER_SECOND = 4
_LIVE_RENDER_INTERVAL = 1 / _LIVE_REFRESH_PER_SECOND
_BATCH_OPERATOR_PANELS = ("queue", "active", "completed", "failed", "scheduler", "logs", "summary")
_VIEW_OPERATOR_KEYS = {"?", "tab", "up", "down"}
_BATCH_VIEW_KEYMAP = OperatorKeymap(
    (
        OperatorAction("↑/↓", "Move", "move the focused batch item", "panels"),
        OperatorAction("tab", "Panels", "cycle batch panels", "panels"),
        OperatorAction("?", "Help", "show batch shortcuts", "panels"),
    )
)
_BATCH_LIVE_KEYMAP = OperatorKeymap(
    (
        *_BATCH_VIEW_KEYMAP.actions,
        OperatorAction("g", "Pause all", "pause or resume new queue starts", "live"),
        OperatorAction("h", "Pause host", "pause or resume the focused host", "live"),
        OperatorAction("s", "Pause item", "pause or resume the focused queued item", "live"),
        OperatorAction("x", "Cancel item", "request cancellation for the focused item", "live"),
        OperatorAction(
            "X",
            "Cancel all",
            "request cancellation for all work",
            "live",
        ),
    )
)


@dataclass(frozen=True)
class ProgressSnapshot:
    completed: int
    total: int | None
    amount_label: str
    speed_label: str
    eta_label: str

    def finished(self) -> ProgressSnapshot:
        total = self.total if self.total is not None else self.completed
        completed = total if total is not None else self.completed
        amount_label = self.amount_label
        if total in {None, 0} and amount_label in {"done", "finished"}:
            amount_label = ""
        return ProgressSnapshot(
            completed=completed,
            total=total,
            amount_label=amount_label,
            speed_label="",
            eta_label=f"[{ATLAS_SUCCESS_STYLE}]done[/{ATLAS_SUCCESS_STYLE}]",
        )


@dataclass(frozen=True)
class BatchProgressState:
    event: ProgressEvent
    updated_at: float


@dataclass(frozen=True)
class WorkPanelContext:
    """Stable live-panel facts that complement the static plan summary."""

    queue_count: int | None = None
    safety_badges: tuple[str, ...] = ()
    title: str = "atlas"
    kind: HubKind | None = None
    operation: str | None = None
    item_title: str | None = None
    source: str | None = None
    quality: str | None = None
    engine: str | None = None
    output: str | None = None
    mode_label: str | None = None
    backends: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()


class _TerminalKeySource:
    """Non-blocking single-key reader that restores terminal mode on close."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._fd = stream.fileno()
        import termios
        import tty

        self._termios = termios
        self._original_attrs = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def __call__(self) -> str | None:
        import select

        ready, _write, _error = select.select([self._stream], [], [], 0.1)
        if not ready:
            return None
        char = self._stream.read(1)
        if not char:
            return None
        if char != "\x1b":
            return char
        sequence = [char]
        for _index in range(2):
            ready, _write, _error = select.select([self._stream], [], [], 0.002)
            if not ready:
                break
            next_char = self._stream.read(1)
            if not next_char:
                break
            sequence.append(next_char)
        return "".join(sequence)

    def close(self) -> None:
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._original_attrs)


class _OperatorInputLoop:
    """Background key reader for interactive full-progress batch sessions."""

    def __init__(
        self,
        *,
        key_source: OperatorKeySource,
        on_key: Callable[[str], None],
        poll_interval: float = 0.05,
    ) -> None:
        self._key_source = key_source
        self._on_key = on_key
        self._poll_interval = poll_interval
        self._stop = Event()
        self._thread = Thread(target=self._run, name="atlas-batch-keys", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        close = getattr(self._key_source, "close", None)
        if callable(close):
            close()

    def _run(self) -> None:
        while not self._stop.is_set():
            key = self._key_source()
            if key:
                self._on_key(key)
            else:
                self._stop.wait(self._poll_interval)


def _normalize_operator_key(key: str) -> str:
    """Normalize terminal bytes and injected test keys to the shared keymap."""

    return {
        "\t": "tab",
        " ": "space",
        "\r": "enter",
        "\n": "enter",
        "\x1b[A": "up",
        "\x1b[B": "down",
        "\x1bOA": "up",
        "\x1bOB": "down",
        "↑": "up",
        "↓": "down",
    }.get(key, key)


def resolve_progress_mode(
    mode: ProgressMode,
    *,
    console: Console,
    quiet: bool,
    json_output: bool,
) -> ProgressMode:
    """Resolve auto progress mode without mixing live progress into JSON output."""

    if quiet:
        return ProgressMode.none
    if mode != ProgressMode.auto:
        return mode
    if json_output or not console.is_terminal:
        return ProgressMode.none
    return ProgressMode.compact


def should_use_alternate_screen(
    mode: ProgressMode,
    *,
    console: Console,
    plain: bool,
) -> bool:
    """Return whether an interactive live progress surface should use alt-screen."""

    return mode in {ProgressMode.compact, ProgressMode.full} and console.is_terminal and not plain


def _default_operator_key_source(console: Console) -> OperatorKeySource | None:
    if not console.is_terminal or not sys.stdin.isatty():
        return None
    try:
        return _TerminalKeySource(sys.stdin)
    except (ImportError, OSError, AttributeError):
        return None


def _make_progress(console: Console, *, label: str) -> Progress:
    columns: list[Any] = []
    if visual_options().motion:
        columns.append(SpinnerColumn("dots", style=ATLAS_ACTIVE_STYLE, finished_text=""))
    columns.extend(
        [
            TextColumn(f"{label} {{task.fields[phase]}} {{task.fields[title]}}"),
            BarColumn(),
            TextColumn("{task.fields[bytes_label]}", justify="right"),
            TextColumn("{task.fields[speed_label]}", justify="right"),
            TextColumn("{task.fields[eta_label]}", justify="right"),
        ]
    )
    return Progress(
        *columns,
        console=console,
        auto_refresh=visual_options().motion,
        refresh_per_second=_LIVE_REFRESH_PER_SECOND,
        transient=False,
        expand=True,
    )


def _make_live(
    console: Console,
    renderable: RenderableType,
    *,
    alternate_screen: bool,
) -> Live:
    """Create a live surface that stops background repainting in reduced-motion mode."""

    return Live(
        renderable,
        console=console,
        auto_refresh=visual_options().motion,
        refresh_per_second=_LIVE_REFRESH_PER_SECOND,
        transient=False,
        screen=alternate_screen,
    )


def _live_refresh_due(*, now: float, last_rendered_at: float) -> bool:
    return now - last_rendered_at >= _LIVE_RENDER_INTERVAL


def _update_live_render(
    live: Live | None,
    render: Callable[[], RenderableType],
    *,
    last_rendered_at: float,
    force: bool = False,
) -> float:
    if live is None:
        return last_rendered_at
    now = monotonic()
    motion = visual_options().motion
    if force or not motion or _live_refresh_due(now=now, last_rendered_at=last_rendered_at):
        live.update(render(), refresh=not motion)
        return now
    return last_rendered_at


def _render_context_card(
    context: WorkPanelContext,
    events: list[ProgressEvent],
    *,
    default_operation: str,
) -> Panel:
    """Render the compact atlas card shown above progress surfaces."""

    latest = _timeline_anchor(events) if events else None

    operation = context.operation or default_operation

    item_title = context.item_title
    if item_title is None and default_operation != "Batch Download" and latest is not None:
        item_title = _event_title(latest)

    latest_engine = None
    if latest is not None and default_operation != "Batch Download":
        latest_engine = latest.engine.value
    engine = context.engine or latest_engine
    backends = visual_join(context.backends) if context.backends else None
    safety = visual_join(context.safety_badges) if context.safety_badges else None
    subtitle = (
        _compact_title(item_title, limit=72)
        if item_title and operation != item_title
        else None
    )
    fields = _view_fields(
        [
            ("Source", context.source, "info"),
            ("Quality", context.quality, "info"),
            ("Engine", engine, "active"),
            ("Output", context.output, "path"),
            ("Mode", context.mode_label, "info"),
            ("Backends", backends, "info"),
            ("Safety", safety, "info"),
        ]
    )
    return SmartSessionView(title=context.title).header_card(
        heading=operation,
        subtitle=subtitle,
        fields=fields,
    )


def _render_work_panel(
    context: WorkPanelContext,
    events: list[ProgressEvent],
    *,
    started_at: float,
    queue_active: int | None = None,
) -> Table:
    """Render a compact live status panel."""

    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    grid.add_row(
        f"Active {_queue_label(context.queue_count, queue_active)}",
        f"Speed {_format_bytes(int(_total_event_speed(events)))}/s",
    )
    grid.add_row(
        f"Elapsed {_format_duration(int(monotonic() - started_at))}",
        f"Notes {_badge_label(context.safety_badges)}",
    )
    phase = Text("Phase ")
    phase.append_text(_phase_timeline(events))
    grid.add_row(phase, "")
    return grid


def _render_single_progress_stack(
    events: list[ProgressEvent],
    *,
    started_at: float,
) -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    latest = _timeline_anchor(events) if events else None
    if latest is None:
        grid.add_row(_semantic_pulse_row("Download", "starting"))
        return grid

    download_event = _latest_phase_event(events, ProgressPhase.download) or latest
    grid.add_row(_semantic_event_row("Download", download_event))

    if _has_postprocess_events(events):
        for label, phase, matcher in [
            ("Merge", ProgressPhase.merge, None),
            ("Extract", ProgressPhase.extract, "extractaudio"),
            ("Embed metadata", ProgressPhase.postprocess, "metadata"),
            ("Thumbnail", ProgressPhase.postprocess, "thumbnail"),
            ("Finalize", ProgressPhase.finalize, None),
        ]:
            phase_event = _latest_phase_event(events, phase, message_contains=matcher)
            grid.add_row(_phase_state_row(label, phase_event))
        return grid

    if download_event.fragment_index is not None or download_event.fragment_count is not None:
        grid.add_row(_semantic_fragment_row("Fragments", download_event))
    grid.add_row(_key_value_row("Speed", _speed_eta_label(latest)))
    grid.add_row(
        _phase_detail_row(
            "Phase",
            _phase_plain_label(latest),
            value_style=ATLAS_ACTIVE_STYLE,
            marker=status_glyph("selected"),
        )
    )
    grid.add_row(_next_phase_row(events))
    grid.add_row(_key_value_row("Elapsed", _format_duration(int(monotonic() - started_at))))
    return grid


def _render_media_job_status(
    context: WorkPanelContext,
    events: list[ProgressEvent],
    *,
    started_at: float,
) -> Group:
    kind = context.kind or _media_job_kind(events)
    latest = _timeline_anchor(events) if events else None
    progress_event = _latest_phase_event(events, ProgressPhase.download) or latest
    heading = _media_job_heading(latest)
    blocks: list[RenderableType] = [
        _media_status_card(context, events, heading=heading),
        _media_progress_table(progress_event),
        Text(),
    ]
    steps = context.steps or _default_media_steps(kind)
    if steps:
        blocks.append(Text("Steps", style=ATLAS_ACTIVE_STYLE))
        blocks.extend(_media_step_rows(steps, events))
    blocks.extend(
        [
            Text(),
            _key_value_row("Elapsed", _format_duration(int(monotonic() - started_at))),
        ]
    )
    return Group(*blocks)


def _progress_snapshot(event: ProgressEvent) -> ProgressSnapshot:
    downloaded = event.downloaded_bytes
    total = event.total_bytes
    if downloaded is not None or total is not None:
        completed = downloaded or 0
        if total is not None and completed > total:
            total = completed
        return ProgressSnapshot(
            completed=completed,
            total=total,
            amount_label=_bytes_label(completed, total),
            speed_label=_speed_label(event.speed_bytes_per_sec),
            eta_label=_eta_label(event.eta_seconds),
        )

    if event.percent is not None:
        percent = min(100.0, max(0.0, event.percent))
        return ProgressSnapshot(
            completed=int(percent),
            total=100,
            amount_label=_percent_label(percent),
            speed_label=_speed_label(event.speed_bytes_per_sec),
            eta_label=_eta_label(event.eta_seconds),
        )

    if event.fragment_index is not None or event.fragment_count is not None:
        completed = event.fragment_index or 0
        total = event.fragment_count
        if total is not None and completed > total:
            total = completed
        return ProgressSnapshot(
            completed=completed,
            total=total,
            amount_label=_fragment_label(completed, total),
            speed_label=_speed_label(event.speed_bytes_per_sec),
            eta_label=_eta_label(event.eta_seconds),
        )

    return ProgressSnapshot(
        completed=0,
        total=None,
        amount_label=event.status,
        speed_label="",
        eta_label="",
    )


def _smoothed_progress_event(
    event: ProgressEvent,
    previous: ProgressEvent | None,
) -> ProgressEvent:
    if previous is None or not _event_is_running(event) or not _event_is_running(previous):
        return event
    updates: dict[str, float] = {}
    if event.speed_bytes_per_sec is not None and previous.speed_bytes_per_sec is not None:
        updates["speed_bytes_per_sec"] = _smooth_value(
            previous.speed_bytes_per_sec,
            event.speed_bytes_per_sec,
        )
    if event.eta_seconds is not None and previous.eta_seconds is not None:
        updates["eta_seconds"] = _smooth_value(previous.eta_seconds, event.eta_seconds)
    return event.model_copy(update=updates) if updates else event


def _smooth_value(previous: float, current: float) -> float:
    return previous + (current - previous) * _PROGRESS_SMOOTHING_ALPHA


class RichProgressReporter:
    """Context manager consuming neutral progress events for one download."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        mode: ProgressMode = ProgressMode.compact,
        kind: HubKind | None = None,
        work_context: WorkPanelContext | None = None,
        alternate_screen: bool = False,
    ) -> None:
        self.console = ensure_atlas_theme(console) if console is not None else themed_console()
        self.mode = mode
        self.kind = kind
        self.work_context = work_context
        self.alternate_screen = alternate_screen
        self._progress = _make_progress(self.console, label="")
        self._progress_active = False
        self._live: Live | None = None
        self._task_ids: dict[ProgressPhase, TaskID] = {}
        self.saved_paths: list[str] = []
        self._events: list[ProgressEvent] = []
        self._started_at = monotonic()
        self._last_live_render_at = self._started_at

    def __enter__(self) -> RichProgressReporter:
        if self.mode in {ProgressMode.compact, ProgressMode.full}:
            if self.work_context is None:
                self._progress.__enter__()
                self._progress_active = True
            else:
                self._live = _make_live(
                    self.console,
                    self._render(),
                    alternate_screen=self.alternate_screen,
                )
                self._live.__enter__()
                self._last_live_render_at = monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._live is not None:
            self._last_live_render_at = _update_live_render(
                self._live,
                self._render,
                last_rendered_at=self._last_live_render_at,
                force=True,
            )
            self._live.__exit__(exc_type, exc, traceback)
        elif self._progress_active:
            self._progress.__exit__(exc_type, exc, traceback)

    def hook(self, event: ProgressEvent) -> None:
        if event.kind is None and self.kind is not None:
            event = event.model_copy(update={"kind": self.kind})
        if self.mode == ProgressMode.json:
            self.console.print(event.model_dump_json(exclude_none=True))
            if event.filename and _event_is_done(event) and event.filename not in self.saved_paths:
                self.saved_paths.append(event.filename)
            return
        if self.mode == ProgressMode.none:
            if event.filename and _event_is_done(event) and event.filename not in self.saved_paths:
                self.saved_paths.append(event.filename)
            return
        event = _smoothed_progress_event(event, self._events[-1] if self._events else None)
        self._events.append(event)
        title = _event_title(event)
        snapshot = _progress_snapshot(event)
        phase = _event_phase(event)

        task_id = self._task_ids.get(phase)
        if task_id is None:
            task_id = self._add_task(title, snapshot, event)
            self._task_ids[phase] = task_id

        if _event_is_running(event):
            self._update_task(task_id, title, snapshot, event)
        elif _event_is_done(event):
            if event.filename and event.filename not in self.saved_paths:
                self.saved_paths.append(event.filename)
            self._update_task(task_id, title, snapshot.finished(), event)
        elif _event_is_error(event):
            error = Text()
            error.append("Error", style=ATLAS_ERROR_STYLE)
            error.append(f" {title}")
            self.console.print(error)
        if self._live is not None:
            self._last_live_render_at = _update_live_render(
                self._live,
                self._render,
                last_rendered_at=self._last_live_render_at,
            )

    def _render(self) -> Group:
        if self.work_context is None:
            return Group(self._progress)
        if _is_media_job_context(self.work_context):
            return _render_media_job_status(
                self.work_context,
                self._events,
                started_at=self._started_at,
            )
        return Group(
            _render_context_card(
                self.work_context,
                self._events,
                default_operation="Download",
            ),
            _render_single_progress_stack(self._events, started_at=self._started_at),
        )

    def _add_task(
        self,
        title: str,
        snapshot: ProgressSnapshot,
        event: ProgressEvent,
    ) -> TaskID:
        return self._progress.add_task(
            "download",
            phase=_phase_label(event, full=self.mode == ProgressMode.full),
            title=_compact_title(title),
            total=snapshot.total,
            bytes_label=snapshot.amount_label,
            speed_label=snapshot.speed_label,
            eta_label=snapshot.eta_label,
        )

    def _update_task(
        self,
        task_id: TaskID,
        title: str,
        snapshot: ProgressSnapshot,
        event: ProgressEvent,
    ) -> None:
        self._progress.update(
            task_id,
            phase=_phase_label(event, full=self.mode == ProgressMode.full),
            title=_compact_title(title),
            completed=snapshot.completed,
            total=snapshot.total,
            bytes_label=snapshot.amount_label,
            speed_label=snapshot.speed_label,
            eta_label=snapshot.eta_label,
        )
        if self._progress_active and not visual_options().motion:
            self._progress.refresh()


def create_progress_hook(
    reporter: RichProgressReporter,
    *,
    kind: HubKind | None = None,
) -> ProgressHook:
    """Return a yt-dlp hook that emits neutral events to the Rich reporter."""

    def hook(raw_event: dict[str, Any]) -> None:
        event_kind = kind if kind is not None else getattr(reporter, "kind", None)
        reporter.hook(progress_event_from_ytdlp(raw_event, kind=event_kind))

    return hook


def create_postprocessor_hook(
    reporter: RichProgressReporter,
    *,
    kind: HubKind | None = None,
) -> ProgressHook:
    """Return a yt-dlp postprocessor hook that emits neutral events."""

    def hook(raw_event: dict[str, Any]) -> None:
        event_kind = kind if kind is not None else getattr(reporter, "kind", None)
        reporter.hook(progress_event_from_ytdlp_postprocessor(raw_event, kind=event_kind))

    return hook


class BatchProgressReporter:
    """Context manager consuming neutral progress events for batch items."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        total: int | None = None,
        concurrency: int | None = None,
        mode: ProgressMode = ProgressMode.compact,
        work_context: WorkPanelContext | None = None,
        alternate_screen: bool = False,
        layout_width: int | None = None,
        operator_controller: BatchOperatorController | None = None,
        operator_key_source: OperatorKeySource | None = None,
    ) -> None:
        self.console = ensure_atlas_theme(console) if console is not None else themed_console()
        self.total = total
        self.concurrency = concurrency
        self.mode = mode
        self.work_context = work_context
        self.alternate_screen = alternate_screen
        self.layout_width = layout_width
        self.operator_controller = operator_controller
        self._operator_key_source = operator_key_source
        self._operator_input_loop: _OperatorInputLoop | None = None
        self._last_operator_result: BatchOperatorResult | None = None
        self._show_shortcut_overlay = False
        self._active_operator_panel = "active"
        self._filter_table_by_operator_panel = False
        self._focused_line: int | None = None
        self._live: Live | None = None
        self._states: dict[int, BatchProgressState] = {}
        self._lock = RLock()
        self.saved_paths: dict[int, list[str]] = {}
        self._started_at = monotonic()
        self._last_live_render_at = self._started_at

    def __enter__(self) -> BatchProgressReporter:
        if self.mode in {ProgressMode.compact, ProgressMode.full}:
            self._live = _make_live(
                self.console,
                self._render(),
                alternate_screen=self.alternate_screen,
            )
            self._live.__enter__()
            self._last_live_render_at = monotonic()
            self._start_operator_input()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._live is not None:
            self._stop_operator_input()
            self._last_live_render_at = _update_live_render(
                self._live,
                self._render,
                last_rendered_at=self._last_live_render_at,
                force=True,
            )
            self._live.__exit__(exc_type, exc, traceback)

    def handle_operator_key(self, key: str) -> BatchOperatorResult | None:
        normalized_key = _normalize_operator_key(key)
        if normalized_key in _VIEW_OPERATOR_KEYS:
            with self._lock:
                result = self._handle_view_operator_key_locked(normalized_key)
                self._last_operator_result = result
                if self._live is not None:
                    self._last_live_render_at = _update_live_render(
                        self._live,
                        self._render,
                        last_rendered_at=self._last_live_render_at,
                        force=True,
                    )
            return result
        if self.operator_controller is None:
            return None
        focused_line, focused_host = self._focused_operator_target()
        result = self.operator_controller.apply_key(
            normalized_key,
            focused_line=focused_line,
            focused_host=focused_host,
        )
        with self._lock:
            self._last_operator_result = result
            if self._live is not None:
                self._last_live_render_at = _update_live_render(
                    self._live,
                    self._render,
                    last_rendered_at=self._last_live_render_at,
                    force=True,
                )
        return result

    def _start_operator_input(self) -> None:
        if (
            self.mode != ProgressMode.full
            or self._operator_input_loop is not None
        ):
            return
        key_source = self._operator_key_source or _default_operator_key_source(self.console)
        if key_source is None:
            return

        def on_operator_key(key: str) -> None:
            self.handle_operator_key(key)

        self._operator_input_loop = _OperatorInputLoop(
            key_source=key_source,
            on_key=on_operator_key,
        )
        self._operator_input_loop.start()

    def _stop_operator_input(self) -> None:
        if self._operator_input_loop is None:
            return
        self._operator_input_loop.stop()
        self._operator_input_loop = None

    def seed_entries(
        self,
        entries: Iterable[BatchEntry],
        *,
        kind: HubKind | None = None,
    ) -> None:
        """Pre-populate the live table so queued work is visible immediately."""

        with self._lock:
            for entry in entries:
                if entry.line_no in self._states:
                    continue
                event = ProgressEvent(
                    engine=EngineKind.unknown,
                    status="queued",
                    phase=ProgressPhase.download,
                    kind=kind,
                    url=entry.url,
                    title=_seed_entry_title(entry.url),
                    item_id=str(entry.line_no),
                    line_no=entry.line_no,
                )
                self._states[entry.line_no] = BatchProgressState(
                    event=event,
                    updated_at=monotonic(),
                )
            if self._live is not None:
                self._last_live_render_at = _update_live_render(
                    self._live,
                    self._render,
                    last_rendered_at=self._last_live_render_at,
                    force=True,
                )

    def hook_for(
        self,
        *,
        line_no: int,
        url: str,
        kind: HubKind | None = None,
        postprocessor: bool = False,
    ) -> ProgressHook:
        def hook(raw_event: dict[str, Any]) -> None:
            if postprocessor:
                event = progress_event_from_ytdlp_postprocessor(
                    raw_event,
                    line_no=line_no,
                    url=url,
                    kind=kind,
                )
            else:
                event = progress_event_from_ytdlp(
                    raw_event,
                    line_no=line_no,
                    url=url,
                    kind=kind,
                )
            self.hook(event)

        return hook

    def hook(self, event: ProgressEvent) -> None:
        line_no = event.line_no or 0
        if self.mode == ProgressMode.json:
            self.console.print(event.model_dump_json(exclude_none=True))
        if line_no <= 0:
            return
        with self._lock:
            previous = self._states.get(line_no)
            if previous is not None:
                event = _smoothed_progress_event(event, previous.event)
            if event.filename and _event_is_done(event):
                paths = self.saved_paths.setdefault(line_no, [])
                if event.filename not in paths:
                    paths.append(event.filename)
            self._states[line_no] = BatchProgressState(event=event, updated_at=monotonic())
            if self._live is not None:
                self._last_live_render_at = _update_live_render(
                    self._live,
                    self._render,
                    last_rendered_at=self._last_live_render_at,
                )

    def _render(self) -> Group:
        focused_line = self._focused_line if self.mode == ProgressMode.full else None
        table_states = self._table_states_for_active_panel()
        table: RenderableType
        layout_width = self.layout_width or self.console.width
        if layout_width < _FULL_BATCH_TABLE_MIN_WIDTH:
            table = _render_compact_batch_rows(
                table_states,
                width=layout_width,
                focused_line=focused_line,
            )
        else:
            table = _render_full_batch_table(
                table_states,
                width=layout_width,
                focused_line=focused_line,
            )

        events = [state.event for state in self._states.values()]
        if self.work_context is None:
            no_context_blocks: list[RenderableType] = [
                _render_batch_bars(
                    events,
                    total=self.total,
                    started_at=self._started_at,
                    full_mode=self.mode == ProgressMode.full,
                    concurrency=self.concurrency,
                ),
            ]
            if self.mode == ProgressMode.full:
                no_context_blocks.extend(self._render_operator_blocks())
            no_context_blocks.append(table)
            return Group(*no_context_blocks)
        blocks: list[RenderableType] = [
            _render_context_card(
                self.work_context,
                events,
                default_operation="Batch Download",
            ),
            _render_batch_bars(
                events,
                total=self.total,
                started_at=self._started_at,
                full_mode=self.mode == ProgressMode.full,
                concurrency=self.concurrency,
            ),
        ]
        if self.mode == ProgressMode.full:
            blocks.extend(self._render_operator_blocks())
        blocks.append(table)
        return Group(*blocks)

    def _render_operator_blocks(self) -> list[RenderableType]:
        view = SmartSessionView(
            title=self.work_context.title if self.work_context is not None else "atlas",
            console=self.console,
        )
        blocks: list[RenderableType] = [
            view.panel_tabs(active=self._active_operator_panel, labels=_BATCH_OPERATOR_PANELS),
            _render_batch_operator_hint(
                self._last_operator_result,
                controls_available=self.operator_controller is not None,
            ),
        ]
        if self._show_shortcut_overlay:
            keymap = (
                _BATCH_LIVE_KEYMAP
                if self.operator_controller is not None
                else _BATCH_VIEW_KEYMAP
            )
            blocks.append(view.shortcut_help_overlay(keymap=keymap))
        return blocks

    def _active_count(self) -> int:
        return sum(1 for state in self._states.values() if _event_is_running(state.event))

    def _done_count(self) -> int:
        return sum(1 for state in self._states.values() if _event_is_done(state.event))

    def _failed_count(self) -> int:
        return sum(1 for state in self._states.values() if _event_is_error(state.event))

    def _skipped_count(self) -> int:
        return sum(1 for state in self._states.values() if state.event.status == "skipped")

    def _total_speed(self) -> float:
        return sum(
            state.event.speed_bytes_per_sec or 0.0
            for state in self._states.values()
            if _event_is_running(state.event)
        )

    def _focused_operator_target(self) -> tuple[int | None, str | None]:
        with self._lock:
            if not self._states:
                return None, None
            focused_line = self._focused_line
            if focused_line is not None and focused_line in self._states:
                state = self._states[focused_line]
                return focused_line, _event_host(state.event)
            line_no = self._default_focused_line_locked()
            if line_no is None:
                return None, None
            self._focused_line = line_no
            state = self._states[line_no]
            return line_no, _event_host(state.event)

    def _handle_view_operator_key_locked(self, key: str) -> BatchOperatorResult:
        if key == "?":
            self._show_shortcut_overlay = not self._show_shortcut_overlay
            message = (
                "showing shortcut help"
                if self._show_shortcut_overlay
                else "hid shortcut help"
            )
            return self._operator_result(key, "toggle_help", True, message)
        if key == "tab":
            current_index = _BATCH_OPERATOR_PANELS.index(self._active_operator_panel)
            self._active_operator_panel = _BATCH_OPERATOR_PANELS[
                (current_index + 1) % len(_BATCH_OPERATOR_PANELS)
            ]
            self._filter_table_by_operator_panel = True
            self._focused_line = None
            return self._operator_result(
                key,
                "cycle_panel",
                True,
                f"focused {self._active_operator_panel} panel",
            )
        if key in {"up", "down"}:
            return self._move_focus_locked(key)
        return self._operator_result(key, "unknown", False, f"no view action for {key!r}")

    def _move_focus_locked(self, key: str) -> BatchOperatorResult:
        lines = self._visible_operator_lines_locked()
        if not lines:
            return self._operator_result(key, "move_focus", False, "no visible items")
        current_line = self._focused_line if self._focused_line in lines else None
        if current_line is None:
            current_line = self._default_focused_line_locked()
        if current_line not in lines:
            current_line = lines[0]
        current_index = lines.index(current_line)
        delta = -1 if key == "up" else 1
        self._focused_line = lines[(current_index + delta) % len(lines)]
        title = _compact_title(_event_title(self._states[self._focused_line].event), limit=34)
        return self._operator_result(
            key,
            "move_focus",
            True,
            f"focused item {self._focused_line}: {title}",
        )

    def _default_focused_line_locked(self) -> int | None:
        if not self._states:
            return None
        running = [
            line_no
            for line_no, state in sorted(self._states.items())
            if _event_is_running(state.event)
        ]
        if running:
            return running[0]
        visible = self._visible_operator_lines_locked(fallback_all=False)
        if visible:
            return visible[0]
        return sorted(self._states)[0]

    def _visible_operator_lines_locked(self, *, fallback_all: bool = True) -> list[int]:
        lines = [
            line_no
            for line_no, state in sorted(self._states.items())
            if _event_matches_operator_panel(state.event, self._active_operator_panel)
        ]
        if lines or not fallback_all:
            return lines
        return sorted(self._states)

    def _table_states_for_active_panel(self) -> dict[int, BatchProgressState]:
        if self.mode != ProgressMode.full:
            return dict(self._states)
        if not self._filter_table_by_operator_panel:
            return dict(self._states)
        if self._active_operator_panel not in {"queue", "active", "completed", "failed"}:
            return dict(self._states)
        return {
            line_no: state
            for line_no, state in self._states.items()
            if _event_matches_operator_panel(state.event, self._active_operator_panel)
        }

    def _operator_result(
        self,
        key: str,
        action: str,
        applied: bool,
        message: str,
    ) -> BatchOperatorResult:
        snapshot: dict[str, object] = {}
        if self.operator_controller is not None:
            snapshot = self.operator_controller.control.snapshot()
        snapshot = {
            **snapshot,
            "active_panel": self._active_operator_panel,
            "focused_line": self._focused_line,
            "shortcut_help": self._show_shortcut_overlay,
        }
        return BatchOperatorResult(
            key=key,
            action=action,
            applied=applied,
            message=message,
            snapshot=snapshot,
        )


def create_batch_progress_hook(
    reporter: BatchProgressReporter,
    *,
    line_no: int,
    url: str,
    kind: HubKind | None = None,
) -> ProgressHook:
    """Return a yt-dlp progress hook for one batch input line."""

    return reporter.hook_for(line_no=line_no, url=url, kind=kind)


def create_batch_postprocessor_hook(
    reporter: BatchProgressReporter,
    *,
    line_no: int,
    url: str,
    kind: HubKind | None = None,
) -> ProgressHook:
    """Return a yt-dlp postprocessor hook for one batch input line."""

    return reporter.hook_for(
        line_no=line_no,
        url=url,
        kind=kind,
        postprocessor=True,
    )


def _render_full_batch_table(
    states: dict[int, BatchProgressState],
    *,
    width: int,
    focused_line: int | None = None,
) -> Table:
    line_width = 4
    kind_width = 8
    progress_width = 14
    bytes_width = 17
    speed_width = 10
    eta_width = 10
    engine_width = 8
    separator_count = 7
    fixed_width = (
        line_width
        + kind_width
        + progress_width
        + bytes_width
        + speed_width
        + eta_width
        + engine_width
        + separator_count
    )
    name_width = max(18, width - fixed_width)

    rows = Table.grid(expand=True)
    rows.add_column(ratio=1)
    rows.add_row(
        _full_batch_header(
            kind_width=kind_width,
            name_width=name_width,
            progress_width=progress_width,
            bytes_width=bytes_width,
            speed_width=speed_width,
            eta_width=eta_width,
            engine_width=engine_width,
        )
    )
    divider = "─" if visual_options().unicode else "-"
    rows.add_row(Text(divider * max(40, width - 2), style=ATLAS_MUTED_STYLE))

    if not states:
        rows.add_row(Text("Waiting for batch items", style=ATLAS_PROGRESS_WAITING_STYLE))
        return rows

    for line_no in sorted(states):
        event = states[line_no].event
        snapshot = _progress_snapshot(event)
        rows.add_row(
            _full_batch_row(
                line_no,
                event,
                snapshot,
                kind_width=kind_width,
                name_width=name_width,
                progress_width=progress_width,
                bytes_width=bytes_width,
                speed_width=speed_width,
                eta_width=eta_width,
                engine_width=engine_width,
                focused=line_no == focused_line,
            )
        )
    return rows


def _render_compact_batch_rows(
    states: dict[int, BatchProgressState],
    *,
    width: int,
    focused_line: int | None = None,
) -> Table:
    rows = Table.grid(expand=True)
    rows.add_column(ratio=1)
    if not states:
        rows.add_row(Text("Waiting for batch items", style=ATLAS_PROGRESS_WAITING_STYLE))
        return rows

    for line_no in sorted(states):
        event = states[line_no].event
        snapshot = _progress_snapshot(event)
        rows.add_row(
            _compact_batch_row(
                line_no,
                event,
                snapshot,
                width=width,
                focused=line_no == focused_line,
            )
        )
    return rows


def _full_batch_header(
    *,
    kind_width: int,
    name_width: int,
    progress_width: int,
    bytes_width: int,
    speed_width: int,
    eta_width: int,
    engine_width: int,
) -> Text:
    text = Text(style=ATLAS_MUTED_STYLE)
    text.append("#".rjust(4))
    text.append(" ")
    text.append("Kind".ljust(kind_width))
    text.append(" ")
    text.append("Name".ljust(name_width))
    text.append(" ")
    text.append("Prog".ljust(progress_width))
    text.append(" ")
    text.append("Xfer".rjust(bytes_width))
    text.append(" ")
    text.append("Spd".rjust(speed_width))
    text.append(" ")
    text.append("ETA".rjust(eta_width))
    text.append(" ")
    text.append("Eng".ljust(engine_width))
    return text


def _full_batch_row(
    line_no: int,
    event: ProgressEvent,
    snapshot: ProgressSnapshot,
    *,
    kind_width: int,
    name_width: int,
    progress_width: int,
    bytes_width: int,
    speed_width: int,
    eta_width: int,
    engine_width: int,
    focused: bool = False,
) -> Text:
    row = Text()
    line_label = f">{line_no}" if focused else f" {line_no}"
    row.append(line_label.rjust(4), style=ATLAS_ACTIVE_STYLE if focused else ATLAS_MUTED_STYLE)
    row.append(" ", style=ATLAS_MUTED_STYLE)
    row.append(
        _compact_title(_batch_kind_label(event), limit=kind_width).ljust(kind_width),
        style=ATLAS_MUTED_STYLE,
    )
    row.append(" ", style=ATLAS_MUTED_STYLE)
    row.append(
        _compact_title(_event_title(event), limit=name_width).ljust(name_width),
        style=ATLAS_ACTIVE_STYLE if focused else "",
    )
    row.append(" ", style=ATLAS_MUTED_STYLE)
    row.append_text(_pad_text(_batch_progress_bar_cell(event, snapshot), progress_width))
    row.append(" ", style=ATLAS_MUTED_STYLE)
    row.append(
        _full_batch_cell(
            _batch_transfer_amount_label(event, snapshot),
            bytes_width,
            align="right",
        )
    )
    row.append(" ", style=ATLAS_MUTED_STYLE)
    row.append(_full_batch_cell(_row_speed_label(event), speed_width, align="right"))
    row.append(" ", style=ATLAS_MUTED_STYLE)
    row.append(_full_batch_cell(_row_eta_label(event), eta_width, align="right"))
    row.append(" ", style=ATLAS_MUTED_STYLE)
    row.append(_full_batch_cell(_batch_backend_label(event), engine_width), style=ATLAS_MUTED_STYLE)
    return row


def _full_batch_cell(value: str, width: int, *, align: str = "left") -> str:
    compact = _compact_title(value, limit=width)
    return compact.rjust(width) if align == "right" else compact.ljust(width)


def _pad_text(value: Text | str, width: int) -> Text:
    text = value.copy() if isinstance(value, Text) else Text(str(value))
    plain = text.plain
    if len(plain) > width:
        return Text(_compact_title(plain, limit=width))
    if len(plain) < width:
        text.append(" " * (width - len(plain)))
    return text


def _compact_batch_row(
    line_no: int,
    event: ProgressEvent,
    snapshot: ProgressSnapshot,
    *,
    width: int,
    focused: bool = False,
) -> Text:
    line_prefix = (f">{line_no}" if focused else f" {line_no}").ljust(4)
    prefix = f"{line_prefix} {_batch_kind_label(event):<5} "
    detail = _compact_batch_detail(event, snapshot, width=width)
    progress = _compact_batch_progress(event, snapshot)
    if width < 64:
        title = _compact_title(_event_title(event), limit=max(8, width - len(prefix)))
        row = Text(prefix, style=ATLAS_MUTED_STYLE)
        row.append(title)
        row.append("\n")
        row.append_text(progress)
        if detail:
            row.append("\n")
            row.append(detail, style=ATLAS_MUTED_STYLE)
        return row

    title_budget = width - len(prefix) - len(progress.plain) - len(detail) - 4
    title = _compact_title(_event_title(event), limit=max(8, min(26, title_budget)))

    row = Text(prefix, style=ATLAS_MUTED_STYLE)
    row.append(title)
    row.append(" ")
    row.append_text(progress)
    if detail:
        row.append(f"  {detail}", style=ATLAS_MUTED_STYLE)
    return row


def _compact_batch_detail(event: ProgressEvent, snapshot: ProgressSnapshot, *, width: int) -> str:
    if event.status == "retrying":
        return ""
    details: list[str] = []
    if (
        width >= 84
        and
        (event.downloaded_bytes is not None or event.total_bytes is not None)
        and snapshot.amount_label not in {event.status, "done", "finished", "queued", "skipped"}
    ):
        details.append(_compact_transfer_label(event))
    details.extend(
        value
        for value in (
            _row_speed_label(event, empty=""),
            _row_eta_label(event, empty=""),
        )
        if value
    )
    if details:
        return "  ".join(details)
    if event.status in {"queued", "retrying", "skipped", "failed"}:
        return event.status
    return ""


def _compact_batch_progress(event: ProgressEvent, snapshot: ProgressSnapshot) -> Text:
    if event.status == "retrying":
        text = _pulse_bar_text(width=6, style=ATLAS_WARNING_STYLE)
        text.append(f" retry {event.retry_count or 1}", style=ATLAS_WARNING_STYLE)
        return text
    if event.downloaded_bytes is not None and event.total_bytes is None:
        text = _pulse_bar_text(width=6, style=ATLAS_PROGRESS_ACTIVE_STYLE)
        text.append(" stream", style=ATLAS_ACTIVE_STYLE)
        return text
    if (
        event.downloaded_bytes is not None
        and event.total_bytes is not None
        and event.total_bytes > 0
    ):
        percent = min(100, max(0, int((event.downloaded_bytes / event.total_bytes) * 100)))
        return _batch_bar_label(
            percent,
            style=_bar_style_for_event(event, fallback=ATLAS_ACTIVE_STYLE),
        )
    if event.percent is not None:
        percent = min(100, max(0, int(event.percent)))
        return _batch_bar_label(
            percent,
            style=_bar_style_for_event(event, fallback=ATLAS_ACTIVE_STYLE),
        )
    if _event_is_done(event):
        return Text("done", style=ATLAS_SUCCESS_STYLE)
    if _event_is_error(event):
        return Text("failed", style=ATLAS_ERROR_STYLE)
    return Text(snapshot.amount_label or event.status or "-", style=ATLAS_PROGRESS_WAITING_STYLE)


def _compact_title(value: str, *, limit: int = 52) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."


def _seed_entry_title(url: str) -> str:
    parsed = urlparse(url)
    last_segment = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    return last_segment or parsed.hostname or url


def _event_host(event: ProgressEvent) -> str | None:
    value = event.url or event.filename
    return urlparse(value).hostname if value else None


def _event_matches_operator_panel(event: ProgressEvent, panel: str) -> bool:
    if panel == "queue":
        return event.status in {"queued", "planned", "starting"}
    if panel == "active":
        return _event_is_running(event) or _event_is_warning(event)
    if panel == "completed":
        return _event_is_done(event) or event.status == "skipped"
    if panel == "failed":
        return _event_is_error(event) or event.status == "canceled"
    return True


class FileProgressReporter:
    """Progress reporter for file/site backends using neutral events."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        title: str = "download",
        mode: ProgressMode = ProgressMode.compact,
        work_context: WorkPanelContext | None = None,
        alternate_screen: bool = False,
    ) -> None:
        self.console = ensure_atlas_theme(console) if console is not None else themed_console()
        self.mode = mode
        self.work_context = work_context
        self.alternate_screen = alternate_screen
        self._progress = _make_progress(
            self.console,
            label=f"[{ATLAS_ACTIVE_STYLE}]Downloading[/{ATLAS_ACTIVE_STYLE}]",
        )
        self._progress_active = False
        self._live: Live | None = None
        self._title = _compact_title(title)
        self._task_id: TaskID | None = None
        self._events: list[ProgressEvent] = []
        self._started_at = monotonic()
        self._last_live_render_at = self._started_at

    def __enter__(self) -> FileProgressReporter:
        if self.mode in {ProgressMode.compact, ProgressMode.full}:
            if self.work_context is None:
                self._progress.__enter__()
                self._progress_active = True
            else:
                self._live = _make_live(
                    self.console,
                    self._render(),
                    alternate_screen=self.alternate_screen,
                )
                self._live.__enter__()
                self._last_live_render_at = monotonic()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._live is not None:
            self._last_live_render_at = _update_live_render(
                self._live,
                self._render,
                last_rendered_at=self._last_live_render_at,
                force=True,
            )
            self._live.__exit__(exc_type, exc, traceback)
        elif self._progress_active:
            self._progress.__exit__(exc_type, exc, traceback)

    def handle_event(self, event: ProgressEvent) -> None:
        if self.mode == ProgressMode.json:
            self.console.print(event.model_dump_json(exclude_none=True))
            return
        if self.mode == ProgressMode.none:
            return
        event = _smoothed_progress_event(event, self._events[-1] if self._events else None)
        self._events.append(event)
        title = _compact_title(_event_title(event))
        snapshot = _progress_snapshot(event)
        if self._task_id is None:
            self._task_id = self._progress.add_task(
                "download",
                phase=_phase_label(event, full=self.mode == ProgressMode.full),
                title=title or self._title,
                total=snapshot.total,
                bytes_label=snapshot.amount_label,
                speed_label=snapshot.speed_label,
                eta_label=snapshot.eta_label,
            )
        if _event_is_done(event):
            snapshot = snapshot.finished()
        self._progress.update(
            self._task_id,
            phase=_phase_label(event, full=self.mode == ProgressMode.full),
            title=title or self._title,
            completed=snapshot.completed,
            total=snapshot.total,
            bytes_label=snapshot.amount_label,
            speed_label=snapshot.speed_label,
            eta_label=snapshot.eta_label,
        )
        if self._progress_active and not visual_options().motion:
            self._progress.refresh()
        if self._live is not None:
            self._last_live_render_at = _update_live_render(
                self._live,
                self._render,
                last_rendered_at=self._last_live_render_at,
            )

    def _render(self) -> Group:
        if self.work_context is None:
            return Group(self._progress)
        if _is_media_job_context(self.work_context):
            return _render_media_job_status(
                self.work_context,
                self._events,
                started_at=self._started_at,
            )
        return Group(
            _render_context_card(
                self.work_context,
                self._events,
                default_operation="Download",
            ),
            _render_single_progress_stack(self._events, started_at=self._started_at),
        )


def _event_phase(event: ProgressEvent) -> ProgressPhase:
    return event.phase


def _event_title(event: ProgressEvent) -> str:
    return event.title or event.filename or event.url or event.message or "download"


def _is_media_job_context(context: WorkPanelContext) -> bool:
    return context.kind in {HubKind.audio, HubKind.video}


def _media_job_kind(events: list[ProgressEvent]) -> HubKind:
    latest = _timeline_anchor(events) if events else None
    if latest and latest.kind in {HubKind.audio, HubKind.video}:
        return latest.kind
    return HubKind.video


def _media_job_breadcrumb(kind: HubKind) -> str:
    label = "Extract audio" if kind == HubKind.audio else "Download video"
    return f"Media \u203a {label}"


def _media_job_heading(event: ProgressEvent | None) -> str:
    if event is None:
        return "Downloading"
    if _event_is_error(event):
        return "Download failed"
    if event.phase in {ProgressPhase.finalize, ProgressPhase.done}:
        return "Finalizing"
    return "Downloading"


def _media_status_card(
    context: WorkPanelContext,
    events: list[ProgressEvent],
    *,
    heading: str,
) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column(ratio=1)
    rows = [
        ("Title", _compact_title(_media_job_title(context, events), limit=82)),
        ("Source", context.source),
        ("Quality", context.quality),
        ("Output", context.output),
    ]
    for label, value in rows:
        if value:
            table.add_row(label, value)
    return Panel(
        table,
        title=Text(heading, style=ATLAS_TITLE_STYLE),
        title_align="left",
        border_style=ATLAS_PANEL_STYLE,
        box=atlas_box(),
        expand=True,
    )


def _media_job_title(context: WorkPanelContext, events: list[ProgressEvent]) -> str:
    if context.item_title:
        return context.item_title
    ignored = {
        "metadata",
        "yt-dlp downloader",
        "aria2c external downloader",
        "download",
    }
    for event in reversed(events):
        title = _event_title(event)
        if title.lower() not in ignored:
            return title
    return ""


def _default_media_steps(kind: HubKind) -> tuple[str, ...]:
    if kind == HubKind.audio:
        return ("Download audio", "Embed metadata", "Add artwork", "Finalize")
    return ("Download video", "Merge video/audio", "Embed metadata", "Add thumbnail", "Finalize")


def _indented_text(value: str, *, style: str = "") -> Text:
    text = Text("  ")
    text.append(value, style=style)
    return text


def _media_progress_table(event: ProgressEvent | None) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_row(_media_progress_bar("Download", event))
    table.add_row(_media_speed_eta_row(event))
    return table


def _media_progress_bar(label: str, event: ProgressEvent | None) -> Text:
    text = _media_progress_label(label)
    if event is None:
        text.append_text(_pulse_bar_text(width=24))
        text.append("  starting", style=ATLAS_MUTED_STYLE)
        return text
    snapshot = _progress_snapshot(event)
    if snapshot.total and snapshot.total > 0:
        percent = min(100, max(0, int((snapshot.completed / snapshot.total) * 100)))
        text.append_text(
            _bar_text(
                percent,
                width=24,
                style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_MEDIA_STYLE),
            )
        )
        text.append(f"  {percent:3d}%")
        text.append(f"   {snapshot.amount_label}", style=ATLAS_MUTED_STYLE)
        return text
    if event.percent is not None:
        percent = min(100, max(0, int(event.percent)))
        text.append_text(
            _bar_text(
                percent,
                width=24,
                style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_MEDIA_STYLE),
            )
        )
        text.append(f"  {percent:3d}%")
        if snapshot.amount_label:
            text.append(f"   {snapshot.amount_label}", style=ATLAS_MUTED_STYLE)
        return text
    text.append_text(
        _pulse_bar_text(
            width=24,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_MEDIA_STYLE),
        )
    )
    detail = event.message or _phase_plain_label(event).lower()
    text.append(f"  {detail}", style=ATLAS_MUTED_STYLE)
    return text


def _media_progress_label(label: str) -> Text:
    text = Text(f"{label:<12}", style=ATLAS_MUTED_STYLE)
    return text


def _media_speed_eta_row(event: ProgressEvent | None) -> Text:
    text = _media_progress_label("Speed")
    if event is None:
        text.append("-", style=ATLAS_MUTED_STYLE)
        return text
    snapshot = _progress_snapshot(event)
    text.append(snapshot.speed_label or "-", style=ATLAS_MUTED_STYLE)
    if snapshot.eta_label:
        text.append(" " * 30, style=ATLAS_MUTED_STYLE)
        text.append(snapshot.eta_label, style=ATLAS_MUTED_STYLE)
    return text


def _media_progress_metrics(event: ProgressEvent | None) -> Text:
    if event is None:
        return _indented_text("waiting for transfer", style=ATLAS_MUTED_STYLE)
    snapshot = _progress_snapshot(event)
    parts = [
        part
        for part in (
            snapshot.amount_label,
            snapshot.speed_label,
            snapshot.eta_label,
        )
        if part
    ]
    if not parts:
        parts.append(event.message or event.status)
    return _indented_text("   ".join(parts), style=ATLAS_MUTED_STYLE)


def _media_step_rows(steps: tuple[str, ...], events: list[ProgressEvent]) -> list[Text]:
    return [
        _media_step_row(label, _media_step_state(label, events), _media_step_suffix(label, events))
        for label in steps
    ]


def _media_step_row(label: str, state: str, suffix: str = "") -> Text:
    if state == "done":
        marker = status_glyph("success")
        style = ATLAS_SUCCESS_STYLE
    elif state == "active":
        marker = "\u25b8" if visual_options().unicode else ">"
        style = ATLAS_ACTIVE_STYLE
    elif state == "error":
        marker = status_glyph("error")
        style = ATLAS_ERROR_STYLE
    else:
        marker = status_glyph("optional")
        style = ATLAS_MUTED_STYLE
    text = Text("  ")
    text.append(f"{marker} {label:<24}", style=style)
    if suffix:
        text.append(suffix, style=ATLAS_MUTED_STYLE)
    return text


def _media_step_state(label: str, events: list[ProgressEvent]) -> str:
    if not events:
        return "waiting"
    latest = _timeline_anchor(events)
    if latest.phase == ProgressPhase.done and _event_is_done(latest):
        return "done"
    if label.lower() == "download audio":
        extract_event = _latest_phase_event(
            events,
            ProgressPhase.extract,
            message_contains="extractaudio",
        )
        if extract_event is not None:
            return _media_event_state(extract_event)
    phase, matcher = _media_step_phase(label)
    event = _latest_phase_event(events, phase, message_contains=matcher)
    if event is None:
        return "waiting"
    return _media_event_state(event)


def _media_event_state(event: ProgressEvent) -> str:
    if _event_is_error(event):
        return "error"
    if _event_is_running(event):
        return "active"
    if _event_is_done(event):
        return "done"
    return "waiting"


def _media_step_phase(label: str) -> tuple[ProgressPhase, str | None]:
    lowered = label.lower()
    if "download" in lowered:
        return ProgressPhase.download, None
    if "merge" in lowered:
        return ProgressPhase.merge, None
    if "extract" in lowered:
        return ProgressPhase.extract, "extractaudio"
    if "metadata" in lowered:
        return ProgressPhase.postprocess, "metadata"
    if "artwork" in lowered or "thumbnail" in lowered:
        return ProgressPhase.postprocess, "thumbnail"
    return ProgressPhase.finalize, None


def _media_step_suffix(label: str, events: list[ProgressEvent]) -> str:
    phase, matcher = _media_step_phase(label)
    event = _latest_phase_event(events, phase, message_contains=matcher)
    if event is None:
        return ""
    snapshot = _progress_snapshot(event)
    if snapshot.total and snapshot.total > 0:
        return _percent_label((snapshot.completed / snapshot.total) * 100)
    if event.percent is not None:
        return _percent_label(event.percent)
    return ""


def _phase_label(event: ProgressEvent, *, full: bool) -> str:
    label = {
        ProgressPhase.probe: "Probing",
        ProgressPhase.extract: "Extracting",
        ProgressPhase.download: "Downloading",
        ProgressPhase.merge: "Merging",
        ProgressPhase.postprocess: "Postprocess",
        ProgressPhase.verify: "Verifying",
        ProgressPhase.finalize: "Finalizing",
        ProgressPhase.done: "Done",
        ProgressPhase.error: "Error",
    }.get(event.phase, event.phase.value.title())
    phase_style = _phase_style_for_event(event)
    if not full:
        return f"[{phase_style}]{label}[/{phase_style}]"
    kind = f" {event.kind.value}" if event.kind else ""
    return (
        f"[{phase_style}]{label}[/{phase_style}]"
        f"[{ATLAS_MUTED_STYLE}]{kind} {event.engine.value}[/{ATLAS_MUTED_STYLE}]"
    )


def _phase_style_for_event(event: ProgressEvent) -> str:
    if _event_is_error(event) or event.phase == ProgressPhase.error:
        return ATLAS_ERROR_STYLE
    if _event_is_done(event) or event.phase == ProgressPhase.done:
        return ATLAS_SUCCESS_STYLE
    if _event_is_warning(event):
        return ATLAS_WARNING_STYLE
    return ATLAS_ACTIVE_STYLE


def _status_label(event: ProgressEvent) -> str:
    if _event_is_done(event):
        return f"[{ATLAS_SUCCESS_STYLE}]done[/{ATLAS_SUCCESS_STYLE}]"
    if _event_is_error(event):
        return f"[{ATLAS_ERROR_STYLE}]error[/{ATLAS_ERROR_STYLE}]"
    if event.status == "queued":
        return f"[{ATLAS_PROGRESS_WAITING_STYLE}]queued[/{ATLAS_PROGRESS_WAITING_STYLE}]"
    if event.status == "skipped":
        return f"[{ATLAS_WARNING_STYLE}]skipped[/{ATLAS_WARNING_STYLE}]"
    if _event_is_warning(event):
        return f"[{ATLAS_WARNING_STYLE}]{_activity_frame()} {event.status}[/{ATLAS_WARNING_STYLE}]"
    if _event_is_running(event):
        return f"[{ATLAS_ACTIVE_STYLE}]{_activity_frame()} running[/{ATLAS_ACTIVE_STYLE}]"
    return escape_status(event.status)


def _activity_frame() -> str:
    if not visual_options().motion:
        return "-"
    return _ACTIVITY_FRAMES[int(monotonic() * 4) % len(_ACTIVITY_FRAMES)]


def escape_status(status: str) -> str:
    return status.replace("[", "\\[").replace("]", "\\]")


def _event_is_running(event: ProgressEvent) -> bool:
    return event.status in {"starting", "started", "downloading", "processing", "running"}


def _event_is_done(event: ProgressEvent) -> bool:
    return event.status in {"finished", "done"}


def _event_is_error(event: ProgressEvent) -> bool:
    return event.status in {"error", "failed"}


def _event_is_warning(event: ProgressEvent) -> bool:
    return event.status in {"retrying", "backoff"}


def _queue_label(total: int | None, active: int | None) -> str:
    if total is None:
        return "-"
    if active is None:
        return str(total)
    return f"{active}/{total}"


def _badge_label(values: tuple[str, ...]) -> str:
    if not values:
        return "-"
    return " ".join(f"[{ATLAS_ACTIVE_STYLE}]{value}[/{ATLAS_ACTIVE_STYLE}]" for value in values)


def _view_fields(rows: Iterable[tuple[str, str | None, str]]) -> tuple[ViewField, ...]:
    return tuple(
        ViewField(label, str(value), state)
        for label, value, state in rows
        if value is not None and str(value)
    )


def _total_event_speed(events: list[ProgressEvent]) -> float:
    latest = _latest_events_by_item(events)
    return sum(
        event.speed_bytes_per_sec or 0.0
        for event in latest.values()
        if _event_is_running(event)
    )


def _active_event_count(events: list[ProgressEvent]) -> int | None:
    if not events:
        return None
    return sum(1 for event in _latest_events_by_item(events).values() if _event_is_running(event))


def _latest_events_by_item(events: list[ProgressEvent]) -> dict[str, ProgressEvent]:
    latest: dict[str, ProgressEvent] = {}
    for event in events:
        key = event.item_id or str(event.line_no or "") or event.url or event.filename or "single"
        latest[key] = event
    return latest


def _render_batch_operator_hint(
    result: BatchOperatorResult | None = None,
    *,
    controls_available: bool = True,
) -> Text:
    keymap = _BATCH_LIVE_KEYMAP if controls_available else _BATCH_VIEW_KEYMAP
    keys = ("↑/↓", "tab", "?", "g", "h", "s", "x", "X") if controls_available else (
        "↑/↓",
        "tab",
        "?",
    )
    parts: list[str] = []
    for key in keys:
        action = keymap.action_for_key(key)
        if action is not None:
            parts.append(f"{key} {action.label.lower()}")
    text = Text("Shortcuts  ", style=ATLAS_MUTED_STYLE)
    separator = visual_separator()
    text.append(separator.join(parts), style=ATLAS_MUTED_STYLE)
    if result is not None:
        result_style = ATLAS_ACTIVE_STYLE if result.applied else ATLAS_WARNING_STYLE
        text.append("\nStatus     ", style=ATLAS_MUTED_STYLE)
        text.append(result.message, style=result_style)
    return text


def _render_batch_bars(
    events: list[ProgressEvent],
    *,
    total: int | None,
    started_at: float,
    full_mode: bool = False,
    concurrency: int | None = None,
) -> Panel:
    stats = _batch_stats(events, total=total)
    speed_summary = _batch_speed_summary(events, started_at=started_at)
    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    total_items = stats["total"]
    grid.add_row(
        _semantic_ratio_row(
            "Overall",
            stats["done"],
            total_items,
            f"{stats['done']} / {total_items}",
            style=ATLAS_PROGRESS_ACTIVE_STYLE,
        )
    )
    transfer = _transfer_totals(events)
    grid.add_row(_transfer_row("Transfer", transfer))
    for label, kinds, style in [
        ("Files", {HubKind.file, HubKind.manifest}, ATLAS_PROGRESS_FILE_STYLE),
        ("Media", {HubKind.video, HubKind.audio}, ATLAS_PROGRESS_MEDIA_STYLE),
        ("Mirrors", {HubKind.site, HubKind.dir}, ATLAS_PROGRESS_MIRROR_STYLE),
    ]:
        lane_done, lane_total = _lane_counts(events, kinds)
        if lane_total:
            grid.add_row(
                _semantic_ratio_row(
                    label,
                    lane_done,
                    lane_total,
                    f"{lane_done} / {lane_total}",
                    style=style,
                )
            )
    grid.add_row(
        _semantic_ratio_row(
            "Failures",
            stats["failed"],
            max(1, total_items),
            str(stats["failed"]),
            style=ATLAS_ERROR_STYLE if stats["failed"] else ATLAS_PROGRESS_WAITING_STYLE,
        )
    )
    grid.add_row(_key_value_row("Speed", speed_summary))
    grid.add_row(
        _key_value_row(
            "Active",
            visual_join(
                (
                    f"{stats['active']} jobs",
                    _active_connection_summary(events),
                    f"{_retry_count(events)} retries",
                    f"{stats['failed']} failed",
                )
            ),
        )
    )
    scheduler_note = _batch_scheduler_note(events, concurrency=concurrency)
    if full_mode and scheduler_note is not None:
        grid.add_row(_key_value_row("Scheduler", scheduler_note))
    return Panel(
        grid,
        title=Text(" Progress ", style=ATLAS_TITLE_STYLE),
        border_style=ATLAS_PANEL_STYLE,
        box=atlas_box(),
        padding=(0, 1),
    )


def _phase_timeline(events: list[ProgressEvent]) -> Text:
    if not events:
        return Text("Ready", style=ATLAS_PROGRESS_WAITING_STYLE)
    phases = [
        (ProgressPhase.probe, "Probing"),
        (ProgressPhase.extract, "Extracting"),
        (ProgressPhase.download, "Downloading"),
        (ProgressPhase.merge, "Merging"),
        (ProgressPhase.postprocess, "Embedding metadata"),
        (ProgressPhase.verify, "Verifying checksum"),
        (ProgressPhase.finalize, "Finalizing"),
    ]
    latest = _timeline_anchor(events)
    completed = {event.phase for event in events if _event_is_done(event)}
    pieces: list[Text] = []
    for phase, label in phases:
        if latest.phase == phase and _event_is_error(latest):
            pieces.append(_phase_timeline_piece(label, state="error"))
        elif latest.phase == phase and _event_is_running(latest):
            pieces.append(_phase_timeline_piece(label, state="active"))
        elif phase in completed:
            pieces.append(_phase_timeline_piece(label, state="complete"))
        else:
            pieces.append(_phase_timeline_piece(label, state="waiting"))
    if latest.phase == ProgressPhase.done:
        pieces.append(_phase_timeline_piece("Done", state="complete"))
    elif latest.phase == ProgressPhase.error:
        pieces.append(_phase_timeline_piece("Error", state="error"))
    timeline = Text()
    separator = Text(" > ", style=ATLAS_MUTED_STYLE)
    for index, piece in enumerate(pieces):
        if index:
            timeline.append_text(separator)
        timeline.append_text(piece)
    return timeline


def _phase_timeline_piece(label: str, *, state: str) -> Text:
    if state == "complete":
        marker = status_glyph("success")
        style = ATLAS_PROGRESS_COMPLETE_STYLE
    elif state == "error":
        marker = status_glyph("error")
        style = ATLAS_ERROR_STYLE
    elif state == "active":
        marker = status_glyph("transition")
        style = ATLAS_PROGRESS_ACTIVE_STYLE
    else:
        marker = status_glyph("optional")
        style = ATLAS_PROGRESS_WAITING_STYLE
    return Text(f"{marker} {label}", style=style)


def _timeline_anchor(events: list[ProgressEvent]) -> ProgressEvent:
    for event in reversed(events):
        if _event_is_error(event):
            return event
    for event in reversed(events):
        if _event_is_running(event):
            return event
    return events[-1]


def _latest_phase_event(
    events: list[ProgressEvent],
    phase: ProgressPhase,
    *,
    message_contains: str | None = None,
) -> ProgressEvent | None:
    for event in reversed(events):
        if event.phase != phase:
            continue
        if message_contains and message_contains not in (event.message or "").lower():
            continue
        return event
    return None


def _has_postprocess_events(events: list[ProgressEvent]) -> bool:
    return any(
        event.phase in {
            ProgressPhase.merge,
            ProgressPhase.postprocess,
            ProgressPhase.finalize,
            ProgressPhase.verify,
        }
        or (
            event.phase == ProgressPhase.extract
            and "extractaudio" in (event.message or "").lower()
        )
        for event in events
    )


def _semantic_event_row(label: str, event: ProgressEvent) -> Text:
    snapshot = _progress_snapshot(event)
    tail = _progress_snapshot_tail(snapshot)
    if event.downloaded_bytes is not None and event.total_bytes:
        return _semantic_ratio_row(
            label,
            event.downloaded_bytes,
            event.total_bytes,
            snapshot.amount_label,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
            tail=tail,
        )
    if event.percent is not None:
        percent = min(100, max(0, int(event.percent)))
        return _semantic_percent_row(
            label,
            percent,
            _percent_label(event.percent),
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
            tail=tail,
        )
    if event.downloaded_bytes is not None:
        return _semantic_pulse_row(
            label,
            visual_join((_format_bytes(event.downloaded_bytes), "size unknown")),
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
            tail=tail,
        )
    if _event_is_warning(event):
        return _semantic_pulse_row(
            label,
            event.message or event.status,
            style=ATLAS_WARNING_STYLE,
        )
    if _event_is_running(event):
        detail = event.message or _phase_plain_label(event)
        return _semantic_pulse_row(
            label,
            detail,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
        )
    if _event_is_done(event):
        return _semantic_percent_row(label, 100, "done", style=ATLAS_PROGRESS_COMPLETE_STYLE)
    if _event_is_error(event):
        return _semantic_percent_row(label, 0, event.message or "error", style=ATLAS_ERROR_STYLE)
    return _semantic_waiting_row(label, event.status)


def _semantic_fragment_row(label: str, event: ProgressEvent) -> Text:
    completed = event.fragment_index or 0
    total = event.fragment_count
    if total:
        return _semantic_ratio_row(
            label,
            completed,
            total,
            f"{completed} / {total}",
            style=ATLAS_PROGRESS_ACTIVE_STYLE,
        )
    return _semantic_pulse_row(label, f"{completed} fragments")


def _phase_state_row(label: str, event: ProgressEvent | None) -> Text:
    if event is None:
        return _semantic_waiting_row(label, "waiting")
    if _event_is_done(event):
        return _semantic_percent_row(
            label,
            100,
            "done",
            style=ATLAS_PROGRESS_COMPLETE_STYLE,
            label_style=ATLAS_SUCCESS_STYLE,
        )
    if _event_is_warning(event):
        return _semantic_pulse_row(
            label,
            event.message or event.status,
            style=ATLAS_WARNING_STYLE,
            label_style=ATLAS_WARNING_STYLE,
        )
    if _event_is_running(event):
        return _semantic_pulse_row(
            label,
            event.message or event.phase.value,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
            label_style=ATLAS_ACTIVE_STYLE,
            marker=status_glyph("selected"),
        )
    if _event_is_error(event):
        return _semantic_percent_row(
            label,
            0,
            "error",
            style=ATLAS_ERROR_STYLE,
            label_style=ATLAS_ERROR_STYLE,
        )
    return _semantic_waiting_row(label, event.status)


def _key_value_row(label: str, value: str | Text) -> Text:
    text = _row_label(label)
    if isinstance(value, Text):
        text.append_text(value if value.plain else Text("-"))
    else:
        text.append(value or "-")
    return text


def _semantic_waiting_row(
    label: str,
    detail: str,
    *,
    label_style: str = ATLAS_MUTED_STYLE,
    marker: str | None = None,
) -> Text:
    text = _row_label(label, style=label_style, marker=marker)
    text.append(detail if detail else "waiting", style=ATLAS_PROGRESS_WAITING_STYLE)
    return text


def _phase_detail_row(
    label: str,
    detail: str,
    *,
    value_style: str = ATLAS_MUTED_STYLE,
    marker: str | None = None,
) -> Text:
    text = _row_label(
        label,
        style=ATLAS_ACTIVE_STYLE if marker else ATLAS_MUTED_STYLE,
        marker=marker,
    )
    text.append(detail if detail else "-", style=value_style)
    return text


def _semantic_pulse_row(
    label: str,
    detail: str,
    *,
    style: str = ATLAS_PROGRESS_ACTIVE_STYLE,
    tail: str = "",
    label_style: str = ATLAS_MUTED_STYLE,
    marker: str | None = None,
) -> Text:
    text = _row_label(label, style=label_style, marker=marker)
    text.append_text(_pulse_bar_text(style=style))
    text.append(f"  {detail}", style=ATLAS_MUTED_STYLE)
    if tail:
        text.append(f"   {tail}", style=ATLAS_MUTED_STYLE)
    return text


def _semantic_percent_row(
    label: str,
    percent: int,
    detail: str,
    *,
    style: str = ATLAS_PROGRESS_ACTIVE_STYLE,
    tail: str = "",
    label_style: str = ATLAS_MUTED_STYLE,
    marker: str | None = None,
) -> Text:
    text = _row_label(label, style=label_style, marker=marker)
    text.append(_bar_text(percent, style=style))
    text.append(f"  {percent:3d}%")
    if detail:
        text.append(f"   {detail}", style=ATLAS_MUTED_STYLE)
    if tail:
        text.append(f"   {tail}", style=ATLAS_MUTED_STYLE)
    return text


def _semantic_ratio_row(
    label: str,
    completed: int | float,
    total: int | float,
    detail: str,
    *,
    style: str = ATLAS_PROGRESS_ACTIVE_STYLE,
    tail: str = "",
    label_style: str = ATLAS_MUTED_STYLE,
    marker: str | None = None,
) -> Text:
    percent = 0 if total <= 0 else min(100, max(0, int((completed / total) * 100)))
    return _semantic_percent_row(
        label,
        percent,
        detail,
        style=style,
        tail=tail,
        label_style=label_style,
        marker=marker,
    )


def _transfer_row(label: str, transfer: tuple[int, int, int]) -> Text:
    downloaded, total, unknown_downloaded = transfer
    if total > 0:
        return _semantic_ratio_row(
            label,
            downloaded,
            total,
            _bytes_label(downloaded, total),
            style=ATLAS_PROGRESS_COMPLETE_STYLE,
        )
    if downloaded or unknown_downloaded:
        return _semantic_pulse_row(
            label,
            visual_join(
                (_format_bytes(downloaded + unknown_downloaded) + " downloaded", "size unknown")
            ),
        )
    return _semantic_waiting_row(label, "waiting")


def _bar_text(
    percent: int,
    *,
    width: int = _SEMANTIC_BAR_WIDTH,
    style: str = ATLAS_PROGRESS_ACTIVE_STYLE,
) -> Text:
    return semantic_bar_text(percent, width=width, style=style)


def _pulse_bar(*, width: int = 16) -> str:
    return _pulse_bar_text(width=width).plain


def _pulse_bar_text(
    *,
    width: int = 16,
    style: str = ATLAS_PROGRESS_ACTIVE_STYLE,
    hot_width: int = 6,
) -> Text:
    return semantic_pulse_bar_text(width=width, style=style, hot_width=hot_width)


def _row_label(
    label: str,
    *,
    style: str = ATLAS_MUTED_STYLE,
    marker: str | None = None,
) -> Text:
    prefix = f"{marker} " if marker else ""
    width = max(1, 13 - len(prefix))
    text = Text(prefix, style=style)
    text.append(f"{label:<{width}}", style=style)
    return text


def _progress_snapshot_tail(snapshot: ProgressSnapshot) -> str:
    return "  ".join(
        part
        for part in (
            snapshot.speed_label,
            snapshot.eta_label,
        )
        if part
    )


def _bar_style_for_event(event: ProgressEvent, *, fallback: str) -> str:
    if _event_is_error(event):
        return ATLAS_ERROR_STYLE
    if _event_is_done(event):
        return ATLAS_PROGRESS_COMPLETE_STYLE
    if _event_is_warning(event):
        return ATLAS_WARNING_STYLE
    if event.kind in {HubKind.video, HubKind.audio}:
        return ATLAS_PROGRESS_MEDIA_STYLE
    if event.kind in {HubKind.site, HubKind.dir}:
        return ATLAS_PROGRESS_MIRROR_STYLE
    if event.kind in {HubKind.file, HubKind.manifest}:
        return ATLAS_PROGRESS_FILE_STYLE
    return fallback


def _speed_eta_label(event: ProgressEvent) -> str:
    speed = _speed_label(event.speed_bytes_per_sec) or "-"
    eta = _eta_label(event.eta_seconds) or "ETA -"
    return visual_join((speed, eta))


def _phase_plain_label(event: ProgressEvent) -> str:
    return {
        ProgressPhase.probe: "Probing",
        ProgressPhase.extract: "Extracting",
        ProgressPhase.download: "Downloading",
        ProgressPhase.merge: "Merging",
        ProgressPhase.postprocess: "Post-processing",
        ProgressPhase.verify: "Verifying",
        ProgressPhase.finalize: "Finalizing",
        ProgressPhase.done: "Done",
        ProgressPhase.error: "Error",
    }.get(event.phase, event.phase.value)


def _next_phase_label(events: list[ProgressEvent]) -> str:
    latest = _timeline_anchor(events)
    kind = latest.kind if latest else HubKind.auto
    completed = {event.phase for event in events if _event_is_done(event)}
    if kind == HubKind.audio:
        pending = [
            (ProgressPhase.download, "Download audio"),
            (ProgressPhase.postprocess, "Embed metadata"),
            (ProgressPhase.finalize, "Finalize"),
        ]
    elif kind == HubKind.video:
        pending = [
            (ProgressPhase.merge, "Merge video/audio"),
            (ProgressPhase.postprocess, "Embed metadata"),
            (ProgressPhase.finalize, "Finalize"),
        ]
    elif kind in {HubKind.site, HubKind.dir}:
        pending = [
            (ProgressPhase.verify, "Verify"),
            (ProgressPhase.finalize, "Finalize mirror"),
        ]
    else:
        pending = [
            (ProgressPhase.verify, "Verify"),
            (ProgressPhase.finalize, "Finalize"),
        ]
    return visual_join(label for phase, label in pending if phase not in completed) or "-"


def _next_phase_row(events: list[ProgressEvent]) -> Text:
    label = _next_phase_label(events)
    if label == "-":
        return _phase_detail_row(
            "Next",
            "waiting",
            value_style=ATLAS_PROGRESS_WAITING_STYLE,
        )
    return _phase_detail_row(
        "Next",
        label,
        value_style=ATLAS_MUTED_STYLE,
        marker=status_glyph("transition"),
    )


def _batch_stats(events: list[ProgressEvent], *, total: int | None) -> dict[str, int]:
    latest = list(_latest_events_by_item(events).values())
    explicit_total = total if total is not None else len(latest)
    done = sum(1 for event in latest if _event_is_done(event))
    failed = sum(1 for event in latest if _event_is_error(event))
    active = sum(1 for event in latest if _event_is_running(event))
    skipped = sum(1 for event in latest if event.status == "skipped")
    queued = max(0, explicit_total - done - failed - active - skipped)
    return {
        "total": explicit_total,
        "done": done,
        "failed": failed,
        "active": active,
        "queued": queued,
        "skipped": skipped,
    }


def _transfer_totals(events: list[ProgressEvent]) -> tuple[int, int, int]:
    downloaded = 0
    total = 0
    unknown_downloaded = 0
    for event in _latest_events_by_item(events).values():
        if event.total_bytes:
            downloaded += event.downloaded_bytes or 0
            total += event.total_bytes
        elif event.downloaded_bytes:
            unknown_downloaded += event.downloaded_bytes
    return downloaded, total, unknown_downloaded


def _lane_counts(events: list[ProgressEvent], kinds: set[HubKind]) -> tuple[int, int]:
    lane = [
        event
        for event in _latest_events_by_item(events).values()
        if event.kind in kinds
    ]
    return sum(1 for event in lane if _event_is_done(event)), len(lane)


def _active_connection_count(events: list[ProgressEvent]) -> int:
    return sum(
        event.active_connections or event.per_file_segments or 0
        for event in _latest_events_by_item(events).values()
        if _event_is_running(event)
    )


def _active_connection_summary(events: list[ProgressEvent]) -> str:
    active = _active_connection_count(events)
    cap = next(
        (
            event.max_total_connections
            for event in _latest_events_by_item(events).values()
            if event.max_total_connections
        ),
        None,
    )
    if cap:
        return f"{active}/{cap} connections"
    return f"{active} connections"


def _retry_count(events: list[ProgressEvent]) -> int:
    return sum(event.retry_count or 0 for event in _latest_events_by_item(events).values())


def _scheduler_summary(events: list[ProgressEvent], *, concurrency: int | None) -> str:
    latest = list(_latest_events_by_item(events).values())
    queue = next((event.queue_concurrency for event in latest if event.queue_concurrency), None)
    per_host = next(
        (event.per_host_concurrency for event in latest if event.per_host_concurrency),
        None,
    )
    connection_cap = next(
        (event.max_total_connections for event in latest if event.max_total_connections),
        None,
    )
    active = sum(1 for event in latest if _event_is_running(event))
    connections = _active_connection_count(events)
    if not any([queue, per_host, connections, connection_cap, concurrency]):
        return ""
    jobs_cap = queue or concurrency or active
    pieces = [f"jobs {active}/{jobs_cap}"]
    if connections or connection_cap:
        if connection_cap:
            pieces.append(f"connections {connections}/{connection_cap}")
        else:
            pieces.append(f"connections {connections}")
    if per_host:
        pieces.append(f"host cap {per_host}")
    return visual_join(pieces)


def _latest_scheduler_decision(events: list[ProgressEvent]) -> str:
    for event in reversed(events):
        if event.scheduler_decision:
            return event.scheduler_decision
    return ""


def _batch_speed_summary(events: list[ProgressEvent], *, started_at: float) -> str:
    speed = _total_event_speed(events)
    speed_text = f"{_format_bytes(int(speed))}/s total" if speed > 0 else "- total"
    eta_text = _batch_eta_label(events, speed=speed)
    if eta_text:
        return visual_join((speed_text, eta_text))
    elapsed = _format_duration(int(monotonic() - started_at))
    return visual_join((speed_text, f"elapsed {elapsed}"))


def _batch_eta_label(events: list[ProgressEvent], *, speed: float) -> str:
    if speed <= 0:
        return ""
    downloaded, total, _unknown_downloaded = _transfer_totals(events)
    if total <= 0 or downloaded >= total:
        return ""
    remaining = total - downloaded
    if remaining <= 0:
        return ""
    return f"ETA {_format_duration(int(remaining / speed))}"


def _batch_scheduler_note(events: list[ProgressEvent], *, concurrency: int | None) -> Text | None:
    summary = _scheduler_summary(events, concurrency=concurrency)
    decision = _latest_scheduler_decision(events)
    if not summary and not decision:
        return None
    if decision:
        text = Text()
        text.append(decision, style=ATLAS_ACTIVE_STYLE)
        return text
    text = Text()
    text.append(summary)
    return text


def _table_progress_label(event: ProgressEvent, snapshot: ProgressSnapshot) -> str:
    if event.downloaded_bytes is not None or event.total_bytes is not None:
        return snapshot.amount_label
    if event.fragment_index is not None or event.fragment_count is not None:
        return snapshot.amount_label
    if _event_is_done(event) or event.status == "queued":
        return "-"
    if snapshot.amount_label == event.status:
        return "-"
    return snapshot.amount_label


def _batch_transfer_amount_label(event: ProgressEvent, snapshot: ProgressSnapshot) -> str:
    if event.downloaded_bytes is not None and event.total_bytes is not None:
        return _compact_transfer_label(event)
    if event.downloaded_bytes is not None:
        return _format_bytes(event.downloaded_bytes)
    if event.fragment_index is not None or event.fragment_count is not None:
        return snapshot.amount_label
    if event.total_bytes is not None:
        return _format_bytes(event.total_bytes)
    if event.estimated_bytes is not None:
        return f"~{_format_bytes(event.estimated_bytes)}"
    if event.status == "retrying":
        return f"retry {event.retry_count or 1}"
    return "-"


def _compact_transfer_label(event: ProgressEvent) -> str:
    downloaded = event.downloaded_bytes
    total = event.total_bytes
    if downloaded is None:
        return "-"
    if total is None:
        return _format_bytes(downloaded)
    downloaded_value, downloaded_unit = _format_bytes_parts(downloaded)
    total_value, total_unit = _format_bytes_parts(total)
    downloaded_value = _compact_number_text(downloaded_value)
    total_value = _compact_number_text(total_value)
    if downloaded_unit == total_unit:
        return f"{downloaded_value}/{total_value} {downloaded_unit}"
    return f"{downloaded_value} {downloaded_unit}/{total_value} {total_unit}"


def _row_speed_label(event: ProgressEvent, *, empty: str = "-") -> str:
    speed = event.speed_bytes_per_sec
    if speed is None:
        return empty
    value, unit = _format_bytes_parts(int(speed))
    return f"{_compact_number_text(value)} {unit}/s"


def _row_eta_label(event: ProgressEvent, *, empty: str = "-") -> str:
    eta = event.eta_seconds
    if eta is None:
        return empty
    return _format_duration(int(eta))


def _batch_backend_label(event: ProgressEvent) -> str:
    return event.selected_backend or event.engine.value


def _batch_kind_label(event: ProgressEvent) -> str:
    return event.kind.value if event.kind is not None else "-"


def _batch_progress_cell(event: ProgressEvent, snapshot: ProgressSnapshot) -> Text | str:
    if event.status == "retrying":
        text = _pulse_bar_text(width=8, style=ATLAS_WARNING_STYLE)
        text.append(f" retry {event.retry_count or 1}", style=ATLAS_WARNING_STYLE)
        return text
    if event.downloaded_bytes is not None and event.total_bytes is None:
        text = _pulse_bar_text(width=8, style=ATLAS_PROGRESS_ACTIVE_STYLE)
        text.append(" streaming", style=ATLAS_ACTIVE_STYLE)
        return text
    return _batch_table_progress_label(event, snapshot)


def _batch_table_progress_label(event: ProgressEvent, snapshot: ProgressSnapshot) -> Text | str:
    style = _bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE)
    if (
        event.downloaded_bytes is not None
        and event.total_bytes is not None
        and event.total_bytes > 0
    ):
        percent = min(100, max(0, int((event.downloaded_bytes / event.total_bytes) * 100)))
        return _batch_bar_label(percent, style=style)
    if event.percent is not None:
        percent = min(100, max(0, int(event.percent)))
        return _batch_bar_label(percent, style=style)
    if (
        event.fragment_index is not None
        and event.fragment_count is not None
        and event.fragment_count > 0
    ):
        percent = min(100, max(0, int((event.fragment_index / event.fragment_count) * 100)))
        return _batch_bar_label(
            percent,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
            detail=snapshot.amount_label,
        )
    return _table_progress_label(event, snapshot)


def _batch_progress_bar_cell(event: ProgressEvent, snapshot: ProgressSnapshot) -> Text | str:
    if event.status == "retrying":
        return _pulse_bar_text(width=8, style=ATLAS_WARNING_STYLE)
    if event.downloaded_bytes is not None and event.total_bytes is None:
        return _pulse_bar_text(width=8, style=ATLAS_PROGRESS_ACTIVE_STYLE)
    if (
        event.downloaded_bytes is not None
        and event.total_bytes is not None
        and event.total_bytes > 0
    ):
        percent = min(100, max(0, int((event.downloaded_bytes / event.total_bytes) * 100)))
        return _batch_bar_label(
            percent,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
        )
    if event.percent is not None:
        percent = min(100, max(0, int(event.percent)))
        return _batch_bar_label(
            percent,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
        )
    if (
        event.fragment_index is not None
        and event.fragment_count is not None
        and event.fragment_count > 0
    ):
        percent = min(100, max(0, int((event.fragment_index / event.fragment_count) * 100)))
        return _batch_bar_label(
            percent,
            style=_bar_style_for_event(event, fallback=ATLAS_PROGRESS_ACTIVE_STYLE),
        )
    if _event_is_done(event):
        return Text("done", style=ATLAS_SUCCESS_STYLE)
    if _event_is_error(event):
        return Text("failed", style=ATLAS_ERROR_STYLE)
    if _event_is_running(event):
        return Text(_phase_plain_label(event).lower(), style=ATLAS_ACTIVE_STYLE)
    return Text(event.status or "-", style=ATLAS_PROGRESS_WAITING_STYLE)


def _batch_bar_label(percent: int, *, style: str, detail: str = "") -> Text:
    text = _bar_text(percent, width=10, style=style)
    text.append(f" {percent:3d}%")
    if detail:
        text.append(f" {detail}", style=ATLAS_MUTED_STYLE)
    return text


def _bytes_label(downloaded: int, total: int | None) -> str:
    if total is None:
        return _format_bytes(downloaded)
    return f"{_format_bytes(downloaded)} / {_format_bytes(total)}"


def _fragment_label(index: int, total: int | None) -> str:
    if total is None:
        return f"fragment {index}"
    return f"fragment {index}/{total}"


def _percent_label(value: float) -> str:
    return f"{value:.0f}%"


def _speed_label(speed: float | None) -> str:
    if speed is None:
        return ""
    return f"{_format_bytes(int(speed))}/s"


def _eta_label(eta: float | None) -> str:
    if eta is None:
        return ""
    return f"ETA {_format_duration(int(eta))}"


def _format_bytes(value: int) -> str:
    amount, unit = _format_bytes_parts(value)
    return f"{amount} {unit}"


def _format_bytes_parts(value: int) -> tuple[str, str]:
    if value < 1000:
        return str(value), "B"
    units = ("kB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        amount /= 1000
        if amount < 1000:
            return f"{amount:.1f}", unit
    return f"{amount:.1f}", "PB"


def _format_duration(seconds: int) -> str:
    seconds = max(seconds, 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _compact_number_text(value: str) -> str:
    return value[:-2] if value.endswith(".0") else value
