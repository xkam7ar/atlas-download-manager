"""Reusable smart-session Rich renderables for atlas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import StringIO

from rich.console import Console, Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from atlas.theme import (
    ATLAS_ACTIVE_STYLE,
    ATLAS_CHOICE_STYLE,
    ATLAS_DISABLED_STYLE,
    ATLAS_ERROR_STYLE,
    ATLAS_MUTED_STYLE,
    ATLAS_PANEL_STYLE,
    ATLAS_PATH_STYLE,
    ATLAS_PROGRESS_ACTIVE_STYLE,
    ATLAS_PROGRESS_FILE_STYLE,
    ATLAS_PROGRESS_MEDIA_STYLE,
    ATLAS_PROGRESS_MIRROR_STYLE,
    ATLAS_PROGRESS_WAITING_STYLE,
    ATLAS_SUBTITLE_STYLE,
    ATLAS_SUCCESS_STYLE,
    ATLAS_TITLE_STYLE,
    ATLAS_WARNING_STYLE,
    AtlasThemeName,
    atlas_box,
    ensure_atlas_theme,
    semantic_bar_text,
    semantic_pulse_bar_text,
    status_glyph,
    table_box,
    themed_console,
    visual_join,
    visual_options,
)


@dataclass(frozen=True)
class ViewField:
    """A key/value fact rendered in atlas cards and panels."""

    label: str
    value: str
    state: str = "info"


@dataclass(frozen=True)
class ProgressMetric:
    """A semantic progress row in the shared session dashboard."""

    label: str
    percent: int | None
    detail: str = ""
    state: str = "active"


@dataclass(frozen=True)
class ActiveWorkRow:
    """One row in the live active-work table."""

    item: str
    kind: str
    phase: str
    progress: str
    speed: str = "-"
    eta: str = "-"
    engine: str = "-"


@dataclass(frozen=True)
class FailureRow:
    """One actionable failure shown in the failure drawer."""

    item: str
    reason: str
    action: str = "retry failed"


@dataclass(frozen=True)
class OperatorAction:
    """One keyboard or command action shown in operator help overlays."""

    key: str
    label: str
    description: str
    scope: str = "session"
    enabled: bool = True
    state: str = "active"

    def to_view_field(self) -> ViewField:
        state = self.state if self.enabled else "disabled"
        description = f"{self.label}: {self.description}"
        if self.scope:
            description = f"{description} ({self.scope})"
        if not self.enabled:
            description = f"{description} disabled"
        return ViewField(_display_key(self.key), description, state)


@dataclass(frozen=True)
class OperatorKeymap:
    """Reusable keymap contract for TUI help overlays and tests."""

    actions: tuple[OperatorAction, ...]

    def fields(self, *, include_disabled: bool = True) -> tuple[ViewField, ...]:
        return tuple(
            action.to_view_field() for action in self.actions if include_disabled or action.enabled
        )

    def action_for_key(self, key: str) -> OperatorAction | None:
        return next((action for action in self.actions if action.key == key), None)


class SmartSessionView:
    """Build atlas smart-session screens from shared composable renderables."""

    def __init__(
        self,
        *,
        title: str = "atlas",
        subtitle: str | None = None,
        width: int | None = None,
        console: Console | None = None,
    ) -> None:
        self.title = title
        self.subtitle = subtitle
        self.console = (
            ensure_atlas_theme(console) if console is not None else themed_console(width=width)
        )

    def header_card(
        self,
        *,
        heading: str,
        fields: Sequence[ViewField] = (),
        subtitle: str | None = None,
    ) -> Panel:
        narrow = self.console.width < 40
        body = Table.grid(padding=(0, 0) if narrow else (0, 2), expand=narrow)
        if narrow:
            body.add_column(ratio=1, overflow="fold")
            body.add_row(Text(heading, style=ATLAS_ACTIVE_STYLE))
            if subtitle or self.subtitle:
                body.add_row(Text(subtitle or self.subtitle or "", style=ATLAS_SUBTITLE_STYLE))
            for field in fields:
                body.add_row(Text(field.label, style=ATLAS_MUTED_STYLE))
                value = Text("  ")
                value.append_text(_field_value(field))
                body.add_row(value)
        else:
            body.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
            body.add_column(ratio=1)
            body.add_row(Text(heading, style=ATLAS_ACTIVE_STYLE), "")
            if subtitle or self.subtitle:
                body.add_row(
                    "",
                    Text(subtitle or self.subtitle or "", style=ATLAS_SUBTITLE_STYLE),
                )
            for field in fields:
                body.add_row(field.label, _field_value(field))
        return Panel(
            body,
            title=Text(f" {self.title} ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
            expand=narrow,
        )

    def scan_panel(
        self,
        *,
        seed: str,
        boundary: str,
        policy: str,
        metrics: Sequence[ProgressMetric],
        facts: Sequence[ViewField] = (),
    ) -> Panel:
        rows: list[RenderableType] = [
            _field_table(
                (
                    ViewField("Seed", seed, "path"),
                    ViewField("Boundary", boundary),
                    ViewField("Policy", policy),
                )
            )
        ]
        rows.extend(_metric_row(metric, width=self.console.width) for metric in metrics)
        for fact in facts:
            rows.append(_key_value_row(fact))
        return Panel(
            Group(*rows),
            title=Text(" Scan ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
        )

    def plan_preview(
        self,
        *,
        heading: str,
        fields: Sequence[ViewField],
        sections: Mapping[str, Sequence[ViewField]],
        actions: Sequence[str] = (),
    ) -> Group:
        blocks: list[RenderableType] = [self.header_card(heading=heading, fields=fields)]
        for section, rows in sections.items():
            blocks.append(_section_table(section, rows))
        if actions:
            action_text = Text("Actions  ", style=ATLAS_MUTED_STYLE)
            action_text.append(
                "  ".join(f"[{label}]" for label in actions),
                style=ATLAS_CHOICE_STYLE,
            )
            blocks.append(action_text)
        return Group(*blocks)

    def customization_overlay(
        self,
        *,
        title: str,
        description: str,
        options: Sequence[ViewField],
    ) -> Panel:
        body = Table.grid(padding=(0, 2))
        body.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
        body.add_column(ratio=1)
        body.add_row("", Text(description, style=ATLAS_SUBTITLE_STYLE))
        for option in options:
            body.add_row(option.label, _field_value(option))
        return Panel(
            body,
            title=Text(f" {title} ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
            expand=False,
        )

    def progress_dashboard(
        self,
        *,
        heading: str,
        fields: Sequence[ViewField],
        metrics: Sequence[ProgressMetric],
        active_rows: Sequence[ActiveWorkRow] = (),
        scheduler: Sequence[ViewField] = (),
        failures: Sequence[FailureRow] = (),
    ) -> Group:
        blocks: list[RenderableType] = [
            self.header_card(heading=heading, fields=fields),
            self.progress_panel(metrics=metrics),
        ]
        if scheduler:
            blocks.append(self.scheduler_panel(scheduler))
        if active_rows:
            blocks.append(self.active_work_table(active_rows))
        if failures:
            blocks.append(self.failure_drawer(failures))
        return Group(*blocks)

    def active_work_table(self, rows: Sequence[ActiveWorkRow]) -> Table:
        if self.console.width < 72:
            table = Table(
                box=table_box(),
                header_style=ATLAS_MUTED_STYLE,
                show_header=False,
                expand=True,
            )
            table.add_column("Work", overflow="fold")
            for row in rows:
                details = visual_join(
                    tuple(
                        value
                        for value in (
                            row.kind,
                            row.phase,
                            row.progress,
                            row.speed,
                            row.eta,
                            row.engine,
                        )
                        if value and value != "-"
                    )
                )
                content = Text(row.item, style=ATLAS_ACTIVE_STYLE)
                if details:
                    content.append("\n")
                    content.append(details, style=ATLAS_MUTED_STYLE)
                table.add_row(content)
            return table
        table = Table(box=table_box(), header_style=ATLAS_MUTED_STYLE, expand=True)
        table.add_column("Item", ratio=2, overflow="ellipsis")
        table.add_column("Kind", no_wrap=True)
        table.add_column("Phase", no_wrap=True)
        table.add_column("Progress", ratio=1, min_width=18)
        table.add_column("Speed", justify="right", no_wrap=True)
        table.add_column("ETA", justify="right", no_wrap=True)
        table.add_column("Engine", no_wrap=True)
        for row in rows:
            table.add_row(
                row.item,
                row.kind,
                row.phase,
                row.progress,
                row.speed,
                row.eta,
                row.engine,
            )
        return table

    def progress_panel(
        self,
        *,
        metrics: Sequence[ProgressMetric],
        title: str = "Progress",
    ) -> Panel:
        rows: list[RenderableType] = [
            _metric_row(metric, width=self.console.width) for metric in metrics
        ]
        if not rows:
            rows.append(Text("Waiting for progress metrics", style=ATLAS_MUTED_STYLE))
        return Panel(
            Group(*rows),
            title=Text(f" {title} ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
        )

    def scheduler_panel(self, rows: Sequence[ViewField]) -> Panel:
        return Panel(
            _field_table(rows),
            title=Text(" Scheduler ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
        )

    def panel_tabs(self, *, active: str, labels: Sequence[str]) -> Text:
        text = Text("Panels  ", style=ATLAS_MUTED_STYLE)
        for index, label in enumerate(labels):
            if index:
                text.append("  ")
            if label == active:
                text.append(f"[{label}]", style=ATLAS_CHOICE_STYLE)
            else:
                text.append(label, style=ATLAS_MUTED_STYLE)
        return text

    def state_panel(
        self,
        *,
        title: str,
        rows: Sequence[ActiveWorkRow],
        empty: str = "No items",
    ) -> Panel:
        body: RenderableType = (
            self.active_work_table(rows) if rows else Text(empty, style=ATLAS_MUTED_STYLE)
        )
        return Panel(
            body,
            title=Text(f" {title} ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
        )

    def failure_drawer(self, rows: Sequence[FailureRow]) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
        table.add_column(ratio=1)
        for row in rows:
            marker = status_glyph("error")
            table.add_row(
                f"{marker} {row.item}",
                visual_join((escape(row.reason), escape(row.action))),
            )
        return Panel(
            table,
            title=Text(" Failures ", style=ATLAS_ERROR_STYLE),
            border_style=ATLAS_ERROR_STYLE,
            box=atlas_box(),
            padding=(0, 1),
        )

    def final_summary(
        self,
        *,
        heading: str,
        fields: Sequence[ViewField],
        actions: Sequence[str] = (),
    ) -> Panel:
        body = _field_table(fields)
        if actions:
            body.add_row("Next actions", visual_join(actions))
        return Panel(
            body,
            title=Text(f" {status_glyph('success')} {heading} ", style=ATLAS_SUCCESS_STYLE),
            border_style=ATLAS_SUCCESS_STYLE,
            box=atlas_box(),
            padding=(0, 1),
            expand=False,
        )

    def preview_panel(
        self,
        *,
        title: str,
        content: str,
        syntax: str = "json",
    ) -> Panel:
        return Panel(
            Syntax(
                content,
                syntax,
                theme=_syntax_theme(),
                word_wrap=True,
                line_numbers=True,
            ),
            title=Text(f" {title} ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
        )

    def shortcut_help_overlay(
        self,
        shortcuts: Sequence[ViewField | OperatorAction] = (),
        *,
        keymap: OperatorKeymap | None = None,
    ) -> Panel:
        if keymap is not None:
            rows = keymap.fields()
        elif shortcuts:
            rows = tuple(_shortcut_to_field(shortcut) for shortcut in shortcuts)
        else:
            rows = DEFAULT_OPERATOR_KEYMAP.fields()
        return Panel(
            _field_table(rows),
            title=Text(" Shortcuts ", style=ATLAS_TITLE_STYLE),
            border_style=ATLAS_PANEL_STYLE,
            box=atlas_box(),
            padding=(0, 1),
            expand=False,
        )

    def render_to_text(self, renderable: RenderableType, *, width: int = 100) -> str:
        return render_to_text(renderable, width=width)


DEFAULT_OPERATOR_KEYMAP = OperatorKeymap(
    (
        OperatorAction("↑/↓", "Move", "move selection", "menus"),
        OperatorAction("/", "Search", "filter menus, folders, manifests, or logs", "menus"),
        OperatorAction("space", "Toggle", "select playlist items or manifest rows", "selection"),
        OperatorAction("enter", "Select", "open, start, or confirm the focused action", "session"),
        OperatorAction(
            "tab",
            "Panels",
            "cycle queue, active, failed, scheduler, logs, summary",
            "panels",
        ),
        OperatorAction("p", "Preview", "show URL, item, manifest, config, log, or plan", "preview"),
        OperatorAction("?", "Help", "show this help overlay", "session"),
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
        OperatorAction(
            "r",
            "Retry",
            "retry or resume failed, skipped, or canceled work",
            "session",
        ),
        OperatorAction("e", "Export", "export filtered URLs", "session"),
        OperatorAction("o", "Open", "open output folder", "session"),
        OperatorAction("q", "Back", "go back or quit", "session"),
    )
)


def _shortcut_to_field(shortcut: ViewField | OperatorAction) -> ViewField:
    if isinstance(shortcut, OperatorAction):
        return shortcut.to_view_field()
    return shortcut


def _display_key(key: str) -> str:
    if visual_options().unicode:
        return key
    return {
        "↑/↓": "up/down",
    }.get(key, key)


def _syntax_theme() -> str:
    if visual_options().theme == AtlasThemeName.light:
        return "ansi_light"
    return "ansi_dark"


def render_to_text(renderable: RenderableType, *, width: int = 100) -> str:
    """Render any shared atlas view to plain text for snapshots and tests."""

    output = StringIO()
    themed_console(file=output, force_terminal=True, width=width, height=25).print(renderable)
    return output.getvalue()


def _section_table(title: str, rows: Sequence[ViewField]) -> Panel:
    return Panel(
        _field_table(rows),
        title=Text(f" {title} ", style=ATLAS_TITLE_STYLE),
        border_style=ATLAS_PANEL_STYLE,
        box=atlas_box(),
        padding=(0, 1),
        expand=False,
    )


def _field_table(rows: Sequence[ViewField]) -> Table:
    table = Table.grid(padding=(0, 2))
    table.add_column(style=ATLAS_MUTED_STYLE, no_wrap=True)
    table.add_column(ratio=1)
    for row in rows:
        table.add_row(row.label, _field_value(row))
    return table


def _field_value(field: ViewField) -> Text:
    value = Text(field.value)
    field_style = _state_style(field.state)
    if field_style:
        value.stylize(field_style)
    return value


def _key_value_row(field: ViewField) -> Text:
    text = Text(f"{field.label:<13}", style=ATLAS_MUTED_STYLE)
    text.append_text(_field_value(field))
    return text


def _metric_row(metric: ProgressMetric, *, width: int = 80) -> Text:
    marker = _metric_state_marker(metric.state)
    label_width = max(1, 12 - len(marker))
    bar_width = max(8, min(20, width - 36))
    text = Text()
    text.append(f"{marker} ", style=_state_style(metric.state) or ATLAS_MUTED_STYLE)
    text.append(f"{metric.label:<{label_width}}", style=ATLAS_MUTED_STYLE)
    if metric.percent is None:
        text.append_text(_pulse_bar(width=bar_width, state=metric.state))
        if metric.detail:
            text.append(f"  {metric.detail}", style=ATLAS_MUTED_STYLE)
        return text
    percent = min(100, max(0, metric.percent))
    bar_percent = _visible_metric_percent(percent, metric)
    text.append(_bar_text(bar_percent, width=bar_width, state=metric.state))
    text.append(f"  {_metric_percent_label(percent, metric):>3}")
    if metric.detail:
        text.append(f"   {metric.detail}", style=ATLAS_MUTED_STYLE)
    return text


def _metric_state_marker(state: str) -> str:
    if state == "success":
        return status_glyph("success")
    if state == "error":
        return status_glyph("error")
    if state == "warning":
        return status_glyph("warning")
    if state in {"waiting", "muted", "disabled"}:
        return status_glyph("optional")
    if state == "info":
        return "i"
    return status_glyph("transition")


def _visible_metric_percent(percent: int, metric: ProgressMetric) -> int:
    if percent == 0 and metric.state in {"error", "warning"} and _detail_is_nonzero(metric.detail):
        return 1
    return percent


def _metric_percent_label(percent: int, metric: ProgressMetric) -> str:
    if percent == 0 and metric.state in {"error", "warning"} and _detail_is_nonzero(metric.detail):
        return "<1%"
    return f"{percent}%"


def _detail_is_nonzero(detail: str) -> bool:
    cleaned = detail.strip()
    if not cleaned:
        return False
    first = cleaned.split(maxsplit=1)[0].replace(",", "")
    try:
        return float(first) > 0
    except ValueError:
        return True


def _bar_text(percent: int, *, width: int = 24, state: str = "active") -> Text:
    bar_style = _state_style(state) or ATLAS_PROGRESS_ACTIVE_STYLE
    return semantic_bar_text(
        percent,
        width=width,
        style=bar_style,
        shimmer=state not in {"success", "error", "warning", "waiting", "muted"},
        min_nonzero=True,
    )


def _pulse_bar(*, width: int = 16, state: str = "active") -> Text:
    pulse_style = _state_style(state) or ATLAS_PROGRESS_ACTIVE_STYLE
    return semantic_pulse_bar_text(width=width, style=pulse_style)


def _state_style(state: str) -> str:
    return {
        "info": "",
        "path": ATLAS_PATH_STYLE,
        "active": ATLAS_PROGRESS_ACTIVE_STYLE,
        "success": ATLAS_SUCCESS_STYLE,
        "warning": ATLAS_WARNING_STYLE,
        "error": ATLAS_ERROR_STYLE,
        "muted": ATLAS_MUTED_STYLE,
        "disabled": ATLAS_DISABLED_STYLE,
        "waiting": ATLAS_PROGRESS_WAITING_STYLE,
        "file": ATLAS_PROGRESS_FILE_STYLE,
        "media": ATLAS_PROGRESS_MEDIA_STYLE,
        "mirror": ATLAS_PROGRESS_MIRROR_STYLE,
    }.get(state, "")
