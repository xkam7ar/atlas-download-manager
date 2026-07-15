from __future__ import annotations

from io import StringIO

from rich.console import Console

from atlas.theme import (
    ATLAS_ACTIVE_STYLE,
    ATLAS_CHOICE_STYLE,
    ATLAS_DISABLED_STYLE,
    ATLAS_PROGRESS_MEDIA_STYLE,
    ATLAS_PROGRESS_SHIMMER_STYLE,
    ATLAS_PROGRESS_WAITING_STYLE,
    ATLAS_WARNING_STYLE,
    AtlasThemeName,
    bar_glyphs,
    configure_visuals,
    ensure_atlas_theme,
    questionary_style_map,
    resolve_theme,
    semantic_bar_text,
    semantic_pulse_bar_text,
    status_glyph,
    themed_console,
    visual_join,
    visual_options,
    visual_separator,
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


def test_plain_visuals_disable_color_and_unicode() -> None:
    try:
        options = configure_visuals(plain=True, env={})

        assert options.plain is True
        assert options.color is False
        assert options.unicode is False
        assert options.motion is False
        assert visual_options() == options
        assert bar_glyphs().complete == "#"
        assert status_glyph("success") == "OK"
        assert status_glyph("selected") == ">"
    finally:
        _restore_visuals()


def test_visual_separators_follow_the_unicode_policy() -> None:
    try:
        configure_visuals(unicode=True, color=True, plain=False, env={})
        assert visual_separator() == " · "
        assert visual_join(("one", "two")) == "one · two"

        configure_visuals(plain=True, env={})
        assert visual_separator() == " | "
        assert visual_join(("one", "two")) == "one | two"
    finally:
        _restore_visuals()


def test_unicode_visuals_use_polished_selected_marker() -> None:
    try:
        configure_visuals(unicode=True, color=True, plain=False, env={})

        assert status_glyph("selected") == "\u203a"
    finally:
        _restore_visuals()


def test_no_color_environment_keeps_unicode_but_disables_color() -> None:
    try:
        options = configure_visuals(color=True, unicode=True, env={"NO_COLOR": "1"})

        assert options.color is False
        assert options.unicode is True
        assert options.motion is True
        assert bar_glyphs().complete == "█"
        assert status_glyph("selected") == "\u203a"
    finally:
        _restore_visuals()


def test_dumb_terminal_disables_color_and_unicode() -> None:
    try:
        options = configure_visuals(color=True, unicode=True, env={"TERM": "dumb"})

        assert options.color is False
        assert options.unicode is False
        assert options.motion is False
        assert bar_glyphs().empty == "-"
    finally:
        _restore_visuals()


def test_no_animation_environment_disables_motion_only() -> None:
    try:
        options = configure_visuals(
            color=True,
            unicode=True,
            motion=True,
            env={"ATLAS_NO_ANIMATION": "1"},
        )

        assert options.color is True
        assert options.unicode is True
        assert options.motion is False
    finally:
        _restore_visuals()


def test_themed_console_strips_color_when_disabled() -> None:
    try:
        configure_visuals(color=False, unicode=False, env={})
        output = StringIO()
        themed_console(file=output, force_terminal=True).print("[atlas.success]done[/]")

        assert output.getvalue().strip() == "done"
        assert bar_glyphs().complete == "#"
    finally:
        _restore_visuals()


def test_ensure_atlas_theme_refreshes_reused_console_after_theme_change() -> None:
    try:
        configure_visuals(theme=AtlasThemeName.dark, color=True, unicode=True, env={})
        console = themed_console(file=StringIO(), force_terminal=True)

        assert str(console.get_style("atlas.active")) == "cyan"

        configure_visuals(theme=AtlasThemeName.light, color=True, unicode=True, env={})
        ensure_atlas_theme(console)

        assert str(console.get_style("atlas.active")) == "blue"

        configure_visuals(
            theme=AtlasThemeName.high_contrast,
            color=True,
            unicode=True,
            env={},
        )
        ensure_atlas_theme(console)

        assert str(console.get_style("atlas.active")) == "bold bright_cyan"
    finally:
        _restore_visuals()


def test_ensure_atlas_theme_refreshes_reused_console_color_policy() -> None:
    try:
        configure_visuals(theme=AtlasThemeName.dark, color=True, unicode=True, env={})
        output = StringIO()
        console = Console(
            file=output,
            force_terminal=True,
            color_system="standard",
            no_color=False,
        )

        ensure_atlas_theme(console).print("[atlas.success]before[/]")

        assert "\x1b[" in output.getvalue()

        output.truncate(0)
        output.seek(0)
        configure_visuals(theme=AtlasThemeName.dark, color=False, unicode=True, env={})
        ensure_atlas_theme(console).print("[atlas.success]after[/]")
        ensure_atlas_theme(console).print("[atlas.muted]muted[/]")

        assert output.getvalue().splitlines() == ["after", "muted"]
        assert "\x1b[" not in output.getvalue()

        output.truncate(0)
        output.seek(0)
        configure_visuals(theme=AtlasThemeName.dark, color=True, unicode=True, env={})
        ensure_atlas_theme(console).print("[atlas.success]again[/]")

        assert "\x1b[" in output.getvalue()
    finally:
        _restore_visuals()


def test_exported_styles_are_semantic_theme_keys() -> None:
    assert ATLAS_ACTIVE_STYLE == "atlas.active"
    assert ATLAS_CHOICE_STYLE == "atlas.choice"
    assert ATLAS_DISABLED_STYLE == "atlas.disabled"
    assert ATLAS_PROGRESS_MEDIA_STYLE == "atlas.progress.media"
    assert ATLAS_PROGRESS_SHIMMER_STYLE == "atlas.progress.shimmer"


def test_named_themes_remap_progress_palette() -> None:
    light = resolve_theme(AtlasThemeName.light)
    high_contrast = resolve_theme(AtlasThemeName.high_contrast)

    assert light[ATLAS_PROGRESS_MEDIA_STYLE] == "magenta"
    assert light[ATLAS_PROGRESS_SHIMMER_STYLE] == "bold white"
    assert high_contrast[ATLAS_PROGRESS_MEDIA_STYLE] == "bold bright_magenta"
    assert high_contrast[ATLAS_PROGRESS_SHIMMER_STYLE] == "bold bright_white"


def test_questionary_style_map_comes_from_shared_visual_policy() -> None:
    try:
        configure_visuals(theme=AtlasThemeName.dark, color=True, env={})
        dark_style = questionary_style_map()
        assert dark_style["highlighted"] == "fg:#000000 bg:#00d7ff bold"
        assert dark_style["pointer"] == "fg:#00d7ff bold"

        configure_visuals(theme=AtlasThemeName.light, color=True, env={})
        light_style = questionary_style_map()
        assert light_style["highlighted"] == "fg:#ffffff bg:#005fbd bold"
        assert light_style["pointer"] == "fg:#005fbd bold"

        configure_visuals(theme=AtlasThemeName.high_contrast, color=True, env={})
        high_contrast_style = questionary_style_map()
        assert high_contrast_style["highlighted"] == "fg:#000000 bg:#ffff00 bold"
        assert high_contrast_style["pointer"] == "fg:#00ffff bold"

        configure_visuals(color=False, env={})
        plain_style = questionary_style_map()
        assert plain_style["highlighted"] == "reverse bold"
        assert plain_style["pointer"] == "bold"
    finally:
        _restore_visuals()


def test_semantic_bar_text_is_the_shared_known_total_primitive() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )

        bar = semantic_bar_text(
            1,
            width=10,
            style=ATLAS_WARNING_STYLE,
            shimmer=False,
            min_nonzero=True,
        )

        assert bar.plain == "█░░░░░░░░░"
        assert any(str(span.style) == ATLAS_WARNING_STYLE for span in bar.spans)
        assert any(str(span.style) == ATLAS_PROGRESS_WAITING_STYLE for span in bar.spans)
        assert all(str(span.style) != ATLAS_PROGRESS_SHIMMER_STYLE for span in bar.spans)
    finally:
        _restore_visuals()


def test_semantic_pulse_bar_text_is_the_shared_unknown_total_primitive() -> None:
    try:
        configure_visuals(
            theme=AtlasThemeName.auto,
            plain=False,
            unicode=True,
            color=True,
            motion=False,
            env={},
        )

        pulse = semantic_pulse_bar_text(width=10, style=ATLAS_WARNING_STYLE)

        assert pulse.plain == "▓▓▓▓▓▓▒▒▒▒"
        assert any(str(span.style) == ATLAS_WARNING_STYLE for span in pulse.spans)
        assert any(str(span.style) == ATLAS_PROGRESS_WAITING_STYLE for span in pulse.spans)
    finally:
        _restore_visuals()
