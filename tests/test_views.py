from __future__ import annotations

import pytest

from atlas.theme import (
    ATLAS_PROGRESS_MIRROR_STYLE,
    ATLAS_PROGRESS_SHIMMER_STYLE,
    ATLAS_WARNING_STYLE,
    AtlasThemeName,
    configure_visuals,
)
from atlas.views import (
    DEFAULT_OPERATOR_KEYMAP,
    ActiveWorkRow,
    FailureRow,
    OperatorAction,
    OperatorKeymap,
    ProgressMetric,
    SmartSessionView,
    ViewField,
    _bar_text,
    _pulse_bar,
    _syntax_theme,
)


def _restore_visuals() -> None:
    configure_visuals(
        theme=AtlasThemeName.auto,
        plain=False,
        unicode=True,
        color=True,
        motion=True,
        env={},
    )


def test_smart_session_dashboard_plain_snapshot_is_accessible_ascii() -> None:
    try:
        configure_visuals(plain=True, env={})
        view = SmartSessionView(title="atlas", width=100)
        renderable = view.progress_dashboard(
            heading="Smart Mirror",
            fields=(
                ViewField("Seed", "http://textfiles.com/directory.html", "path"),
                ViewField("Mode", "recursive directory mirror"),
                ViewField("Safety", "same-host · no-parent · archive on"),
            ),
            metrics=(
                ProgressMetric("Discovery", 78, "1,842 links found", "mirror"),
                ProgressMetric("Download", 51, "622 / 1,219 files", "file"),
                ProgressMetric("Resolving", None, "probing unknown sizes"),
            ),
            active_rows=(
                ActiveWorkRow(
                    item="archive-1993.zip",
                    kind="file",
                    phase="download",
                    progress="31%",
                    speed="8.4 MB/s",
                    eta="00:08",
                    engine="aria2c",
                ),
            ),
            scheduler=(
                ViewField("Global", "jobs 24/40 · connections 38/96"),
                ViewField("Decision", "small-file lane increased 16 -> 24", "active"),
            ),
            failures=(FailureRow("Line 93", "checksum mismatch"),),
        )

        rendered = view.render_to_text(renderable, width=100)

        assert "Smart Mirror" in rendered
        assert "Progress" in rendered
        assert "Discovery" in rendered
        assert "Scheduler" in rendered
        assert "Failures" in rendered
        assert "archive-1993.zip" in rendered
        assert "#" in rendered
        assert "-" in rendered
        assert "█" not in rendered
        assert "░" not in rendered
        assert "\x1b[" not in rendered
    finally:
        _restore_visuals()


def test_smart_session_metric_bars_use_semantic_shimmer_and_pulse() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            env={},
        )

        active = _bar_text(50, width=10, state="mirror")
        pulse = _pulse_bar(width=10, state="warning")

        assert len(active.plain) == 10
        assert any(char in active.plain for char in {"▓", "▒", "▌"})
        assert any(str(span.style) == ATLAS_PROGRESS_MIRROR_STYLE for span in active.spans)
        assert any(str(span.style) == ATLAS_PROGRESS_SHIMMER_STYLE for span in active.spans)
        assert len(pulse.plain) == 10
        assert "▓" in pulse.plain
        assert "▒" in pulse.plain
        assert any(str(span.style) == ATLAS_WARNING_STYLE for span in pulse.spans)
    finally:
        _restore_visuals()


def test_smart_session_metric_bars_keep_plain_ascii_fallback() -> None:
    try:
        configure_visuals(plain=True, env={})

        active = _bar_text(50, width=10, state="mirror")
        pulse = _pulse_bar(width=10, state="warning")

        assert active.plain.count("#") + active.plain.count("+") == 5
        assert "-" in active.plain
        assert "=" in pulse.plain
        assert "-" in pulse.plain
        assert "█" not in active.plain
        assert "▓" not in pulse.plain
    finally:
        _restore_visuals()


def test_smart_session_metric_bars_disable_motion() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )

        active = _bar_text(50, width=10, state="mirror")
        pulse = _pulse_bar(width=10, state="warning")

        assert active.plain == "█████░░░░░"
        assert all(str(span.style) != ATLAS_PROGRESS_SHIMMER_STYLE for span in active.spans)
        assert pulse.plain == "▓▓▓▓▓▓▒▒▒▒"
    finally:
        _restore_visuals()


def test_smart_session_active_bar_uses_head_glyph_only_while_animating() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=True,
            env={},
        )

        active = _bar_text(82, width=12, state="mirror")
        complete = _bar_text(100, width=12, state="success")

        assert "▌" in active.plain
        assert any(str(span.style) == ATLAS_PROGRESS_SHIMMER_STYLE for span in active.spans)
        assert "▌" not in complete.plain
        assert all(str(span.style) != ATLAS_PROGRESS_SHIMMER_STYLE for span in complete.spans)
    finally:
        _restore_visuals()


def test_smart_session_failure_metric_shows_tiny_nonzero_ratio() -> None:
    try:
        configure_visuals(color=False, unicode=True, env={})
        view = SmartSessionView(title="atlas", width=90)

        rendered = view.render_to_text(
            view.progress_dashboard(
                heading="Batch",
                fields=(ViewField("Queue", "1,284 items"),),
                metrics=(ProgressMetric("Failures", 0, "1 failed", "error"),),
            ),
            width=90,
        )

        assert "Failures" in rendered
        assert "x Failures" in rendered
        assert "<1%" in rendered
        assert "1 failed" in rendered
        assert "█" in rendered
        assert "\x1b[" not in rendered
    finally:
        _restore_visuals()


def test_smart_session_narrow_layout_stacks_work_and_shrinks_metrics() -> None:
    try:
        configure_visuals(color=False, unicode=False, motion=False, env={})
        view = SmartSessionView(title="atlas", width=48)

        work = view.render_to_text(
            view.active_work_table(
                (
                    ActiveWorkRow(
                        item="archive-1993.zip",
                        kind="file",
                        phase="download",
                        progress="31%",
                        speed="8.4 MB/s",
                        eta="00:08",
                        engine="aria2c",
                    ),
                )
            ),
            width=48,
        )
        progress = view.render_to_text(
            view.progress_panel(metrics=(ProgressMetric("Download", 31, "622 files", "file"),)),
            width=48,
        )

        assert "archive-1993.zip" in work
        assert "file" in work
        assert "download" in work
        assert "aria2c" in work
        assert "Kind" not in work
        assert "Download" in progress
        assert "622 files" in progress
        assert max(len(line) for line in (work + progress).splitlines()) <= 48
    finally:
        _restore_visuals()


@pytest.mark.parametrize("width", [20, 24])
def test_header_card_stacks_narrow_fields_without_dropping_safety(width: int) -> None:
    try:
        configure_visuals(color=False, unicode=False, motion=False, env={})
        view = SmartSessionView(title="atlas", width=width)

        rendered = view.render_to_text(
            view.header_card(
                heading="Download",
                fields=(
                    ViewField("Source", "example.com"),
                    ViewField("Safety", "single-host no-parent bounded"),
                ),
            ),
            width=width,
        )

        assert "Safety" in rendered
        assert "single-host" in rendered
        assert "no-parent" in rendered
        assert "bounded" in rendered
        assert max(len(line) for line in rendered.splitlines()) <= width
    finally:
        _restore_visuals()


def test_smart_session_plan_preview_and_summary_use_status_text() -> None:
    try:
        configure_visuals(color=False, unicode=True, env={})
        view = SmartSessionView(title="atlas", subtitle="adaptive scheduler")
        plan = view.plan_preview(
            heading="Smart Mirror Plan",
            fields=(
                ViewField("Seed", "http://textfiles.com/directory.html", "path"),
                ViewField("Backend", "wget2 discovery · atlas adaptive scheduler"),
            ),
            sections={
                "Scope": (
                    ViewField("Recursive", "yes", "success"),
                    ViewField("Depth", "2"),
                ),
                "Scheduler": (
                    ViewField("Mode", "adaptive", "active"),
                    ViewField("Per-host cap", "dynamic, max 6"),
                ),
            },
            actions=("Start", "Customize", "Dry run", "Save manifest"),
        )
        summary = view.final_summary(
            heading="Complete",
            fields=(
                ViewField("Succeeded", "1,251", "success"),
                ViewField("Failed", "4", "warning"),
                ViewField("Manifest", "saved"),
            ),
            actions=("Open folder", "Retry failed", "Quit"),
        )

        rendered = view.render_to_text(plan, width=100)
        rendered += view.render_to_text(summary, width=100)

        assert "Smart Mirror Plan" in rendered
        assert "Scope" in rendered
        assert "[Start]" in rendered
        assert "✓ Complete" in rendered
        assert "Retry failed" in rendered
        assert "\x1b[" not in rendered
    finally:
        _restore_visuals()


def test_smart_session_preview_panel_renders_bat_style_content() -> None:
    try:
        configure_visuals(color=False, unicode=False, env={})
        view = SmartSessionView(title="atlas")
        preview = view.preview_panel(
            title="Manifest JSON",
            content='{"type":"progress","phase":"download"}',
            syntax="json",
        )

        rendered = view.render_to_text(preview, width=80)

        assert "Manifest JSON" in rendered
        assert '"phase"' in rendered
        assert "download" in rendered
        assert "\x1b[" not in rendered
    finally:
        _restore_visuals()


def test_preview_syntax_theme_tracks_light_palette() -> None:
    try:
        configure_visuals(theme=AtlasThemeName.light, color=True, unicode=True, env={})
        assert _syntax_theme() == "ansi_light"

        configure_visuals(theme=AtlasThemeName.dark, color=True, unicode=True, env={})
        assert _syntax_theme() == "ansi_dark"

        configure_visuals(theme=AtlasThemeName.high_contrast, color=True, unicode=True, env={})
        assert _syntax_theme() == "ansi_dark"
    finally:
        _restore_visuals()


def test_shortcut_help_overlay_is_accessible_without_color() -> None:
    try:
        configure_visuals(plain=True, env={})
        view = SmartSessionView(title="atlas")

        rendered = view.render_to_text(view.shortcut_help_overlay(), width=80)

        assert "Shortcuts" in rendered
        assert "?" in rendered
        assert "show this help overlay" in rendered
        assert "/" in rendered
        assert "Search" in rendered
        assert "filter menus" in rendered
        assert "export filtered URLs" in rendered
        assert "up/down" in rendered
        assert "↑" not in rendered
        assert "Cancel item" in rendered
        assert "focused item" in rendered
        assert "\x1b[" not in rendered
    finally:
        _restore_visuals()


def test_operator_keymap_exposes_live_control_actions() -> None:
    cancel = DEFAULT_OPERATOR_KEYMAP.action_for_key("x")
    cancel_all = DEFAULT_OPERATOR_KEYMAP.action_for_key("X")

    assert cancel is not None
    assert cancel.label == "Cancel item"
    assert cancel.scope == "live"
    assert cancel_all is not None
    assert "request cancellation for all work" in cancel_all.description


def test_shortcut_overlay_renders_disabled_actions_as_text() -> None:
    try:
        configure_visuals(plain=True, env={})
        view = SmartSessionView(title="atlas")
        keymap = OperatorKeymap(
            (
                OperatorAction(
                    "x",
                    "Cancel item",
                    "no active item selected",
                    "live",
                    enabled=False,
                ),
            )
        )

        rendered = view.render_to_text(view.shortcut_help_overlay(keymap=keymap), width=80)

        assert "Cancel item" in rendered
        assert "no active item selected" in rendered
        assert "disabled" in rendered
        assert "\x1b[" not in rendered
    finally:
        _restore_visuals()


def test_state_panel_and_tabs_render_lazygit_style() -> None:
    try:
        configure_visuals(plain=True, env={})
        view = SmartSessionView(title="atlas")
        renderable = view.state_panel(
            title="Failed Items",
            rows=(
                ActiveWorkRow(
                    item="https://example.com/bad.iso",
                    kind="file",
                    phase="failed",
                    progress="checksum mismatch",
                    engine="aria2",
                ),
            ),
        )
        rendered = view.render_to_text(view.panel_tabs(active="failed", labels=("queue", "failed")))
        rendered += view.render_to_text(renderable, width=140)

        assert "[failed]" in rendered
        assert "Failed Items" in rendered
        assert "https://example.com/" in rendered
        assert "checksum" in rendered
        assert "mismatch" in rendered
        assert "aria2" in rendered
        assert "\x1b[" not in rendered
    finally:
        _restore_visuals()
