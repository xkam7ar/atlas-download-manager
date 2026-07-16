"""Shared visual policy for the atlas terminal UI."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import sin
from time import monotonic
from typing import Final, Literal, TextIO

from rich import box
from rich.box import Box
from rich.console import Console
from rich.errors import MissingStyle
from rich.text import Text
from rich.theme import Theme


class AtlasThemeName(StrEnum):
    """Named palettes exposed by the global CLI options."""

    auto = "auto"
    dark = "dark"
    light = "light"
    high_contrast = "high-contrast"


@dataclass(frozen=True)
class VisualOptions:
    """Runtime rendering choices that must stay consistent across atlas views."""

    theme: AtlasThemeName = AtlasThemeName.auto
    color: bool = True
    unicode: bool = True
    plain: bool = False
    motion: bool = True


@dataclass(frozen=True)
class BarGlyphs:
    """Progress bar characters for unicode and plain-terminal output."""

    complete: str
    empty: str
    pulse: str
    pulse_hot: str
    shimmer: tuple[str, ...]
    head: str


@dataclass(frozen=True)
class StatusGlyphs:
    """Status markers with text fallbacks so meaning never depends on color."""

    success: str
    warning: str
    error: str
    optional: str
    transition: str
    selected: str


ATLAS_THEME: Final[dict[str, str]] = {
    "atlas.title": "bold cyan",
    "atlas.subtitle": "dim",
    "atlas.panel": "cyan",
    "atlas.path": "dim italic",
    "atlas.success": "green",
    "atlas.warning": "yellow",
    "atlas.error": "bold red",
    "atlas.muted": "dim",
    "atlas.active": "cyan",
    "atlas.disabled": "dim",
    "atlas.choice": "bright_cyan",
    "atlas.danger": "bold yellow",
    "atlas.progress.complete": "green",
    "atlas.progress.active": "cyan",
    "atlas.progress.waiting": "dim",
    "atlas.progress.file": "green",
    "atlas.progress.media": "cyan",
    "atlas.progress.mirror": "blue",
    "atlas.progress.shimmer": "bold white",
}

_LIGHT_THEME: Final[dict[str, str]] = {
    **ATLAS_THEME,
    "atlas.title": "bold blue",
    "atlas.panel": "blue",
    "atlas.path": "dim blue",
    "atlas.active": "blue",
    "atlas.choice": "bright_blue",
    "atlas.progress.active": "blue",
    "atlas.progress.media": "magenta",
    "atlas.progress.mirror": "cyan",
}

_HIGH_CONTRAST_THEME: Final[dict[str, str]] = {
    **ATLAS_THEME,
    "atlas.title": "bold bright_cyan",
    "atlas.subtitle": "white",
    "atlas.panel": "bright_cyan",
    "atlas.path": "bright_cyan",
    "atlas.success": "bold bright_green",
    "atlas.warning": "bold bright_yellow",
    "atlas.error": "bold bright_red",
    "atlas.muted": "white",
    "atlas.active": "bold bright_cyan",
    "atlas.choice": "bold bright_cyan",
    "atlas.progress.complete": "bold bright_green",
    "atlas.progress.active": "bold bright_cyan",
    "atlas.progress.waiting": "white",
    "atlas.progress.file": "bold bright_green",
    "atlas.progress.media": "bold bright_magenta",
    "atlas.progress.mirror": "bold bright_blue",
    "atlas.progress.shimmer": "bold bright_white",
}

_THEMES: Final[dict[AtlasThemeName, dict[str, str]]] = {
    AtlasThemeName.auto: ATLAS_THEME,
    AtlasThemeName.dark: ATLAS_THEME,
    AtlasThemeName.light: _LIGHT_THEME,
    AtlasThemeName.high_contrast: _HIGH_CONTRAST_THEME,
}

_UNICODE_BARS: Final[BarGlyphs] = BarGlyphs(
    complete="█",
    empty="░",
    pulse="▒",
    pulse_hot="▓",
    shimmer=("▓", "▒", "▓", "▒"),
    head="▌",
)
_ASCII_BARS: Final[BarGlyphs] = BarGlyphs(
    complete="#",
    empty="-",
    pulse="-",
    pulse_hot="=",
    shimmer=("#", "+", "#", "+"),
    head=">",
)
_UNICODE_STATUS: Final[StatusGlyphs] = StatusGlyphs(
    success="✓",
    warning="!",
    error="x",
    optional="○",
    transition="→",
    selected="\u203a",
)
_ASCII_STATUS: Final[StatusGlyphs] = StatusGlyphs(
    success="OK",
    warning="!",
    error="x",
    optional="o",
    transition="->",
    selected=">",
)

_OPTIONS = VisualOptions()
_CONSOLE_VISUAL_MARKER_ATTR: Final[str] = "_atlas_visual_marker"
_CONSOLE_COLOR_SYSTEM_ATTR: Final[str] = "_atlas_color_system"


def configure_visuals(
    *,
    theme: AtlasThemeName | str | None = None,
    plain: bool | None = None,
    unicode: bool | None = None,
    color: bool | None = None,
    motion: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> VisualOptions:
    """Set process-wide visual options for menus, progress, and shared views."""

    global _OPTIONS
    values = os.environ if env is None else env
    selected_theme = _coerce_theme(theme) if theme is not None else _OPTIONS.theme
    plain_value = _OPTIONS.plain if plain is None else plain
    color_value = _OPTIONS.color if color is None else color
    unicode_value = _OPTIONS.unicode if unicode is None else unicode
    motion_value = _OPTIONS.motion if motion is None else motion

    if _env_disables_color(values) or plain_value:
        color_value = False
    if _env_disables_unicode(values) or plain_value:
        unicode_value = False
    if _env_disables_motion(values) or plain_value:
        motion_value = False

    _OPTIONS = VisualOptions(
        theme=selected_theme,
        color=color_value,
        unicode=unicode_value,
        plain=plain_value,
        motion=motion_value,
    )
    return _OPTIONS


def visual_options() -> VisualOptions:
    """Return the current process-wide visual options."""

    return _OPTIONS


def visual_separator() -> str:
    """Return the shared inline separator for human terminal output."""

    return " · " if visual_options().unicode else " | "


def visual_join(parts: Iterable[str]) -> str:
    """Join human-facing values without leaking Unicode into plain mode."""

    return visual_separator().join(parts)


def reset_visuals() -> VisualOptions:
    """Reset visual policy to environment-aware defaults."""

    global _OPTIONS
    _OPTIONS = VisualOptions()
    return configure_visuals(
        theme=AtlasThemeName.auto,
        plain=False,
        unicode=True,
        color=True,
        motion=True,
    )


def themed_console(
    *,
    file: TextIO | None = None,
    force_terminal: bool | None = None,
    width: int | None = None,
    height: int | None = None,
) -> Console:
    """Create a Rich console that obeys the current atlas visual policy."""

    options = visual_options()
    console = Console(
        file=file,
        force_terminal=force_terminal,
        width=width,
        height=height,
        theme=Theme(resolve_theme(options.theme)),
        no_color=not options.color,
        color_system="auto" if options.color else None,
    )
    _set_console_visual_marker(console, options)
    return console


def ensure_atlas_theme(console: Console) -> Console:
    """Attach or refresh the active atlas theme on a caller-provided Rich console."""

    options = visual_options()
    selected = options.theme
    _apply_console_color_policy(console, options)
    if getattr(console, _CONSOLE_VISUAL_MARKER_ATTR, None) != _console_visual_marker(options):
        console.push_theme(Theme(resolve_theme(selected)), inherit=True)
        _set_console_visual_marker(console, options)
        return console
    try:
        console.get_style("atlas.muted")
    except MissingStyle:
        console.push_theme(Theme(resolve_theme(selected)), inherit=True)
        _set_console_visual_marker(console, options)
    return console


def _apply_console_color_policy(console: Console, options: VisualOptions) -> None:
    if not hasattr(console, _CONSOLE_COLOR_SYSTEM_ATTR):
        setattr(console, _CONSOLE_COLOR_SYSTEM_ATTR, console._color_system)
    console.no_color = not options.color
    console._color_system = getattr(console, _CONSOLE_COLOR_SYSTEM_ATTR) if options.color else None


def _console_visual_marker(options: VisualOptions) -> tuple[str, bool]:
    return (options.theme.value, options.color)


def _set_console_visual_marker(console: Console, options: VisualOptions) -> None:
    setattr(console, _CONSOLE_VISUAL_MARKER_ATTR, _console_visual_marker(options))


def resolve_theme(theme: AtlasThemeName | str | None = None) -> dict[str, str]:
    """Return the style map for a named atlas theme."""

    selected = _coerce_theme(theme) if theme is not None else visual_options().theme
    return dict(_THEMES[selected])


def style(name: str) -> str:
    """Return a style by semantic name from the current palette."""

    return resolve_theme().get(name, "")


def atlas_box() -> Box:
    """Return unicode or ASCII panel/table boxes for the active terminal mode."""

    return box.ROUNDED if visual_options().unicode else box.ASCII


def table_box() -> Box:
    """Return a compact table box that will not corrupt narrow/plain terminals."""

    return box.SIMPLE_HEAD if visual_options().unicode else box.ASCII2


def bar_glyphs() -> BarGlyphs:
    """Return progress glyphs for the active unicode/plain mode."""

    return _UNICODE_BARS if visual_options().unicode else _ASCII_BARS


def status_glyphs() -> StatusGlyphs:
    """Return status glyphs for the active unicode/plain mode."""

    return _UNICODE_STATUS if visual_options().unicode else _ASCII_STATUS


def status_glyph(
    name: Literal["success", "warning", "error", "optional", "transition", "selected"],
) -> str:
    """Return one named status marker."""

    glyphs = status_glyphs()
    if name == "success":
        return glyphs.success
    if name == "warning":
        return glyphs.warning
    if name == "error":
        return glyphs.error
    if name == "optional":
        return glyphs.optional
    if name == "transition":
        return glyphs.transition
    return glyphs.selected


def questionary_style_map() -> dict[str, str]:
    """Return prompt-toolkit styles for Questionary prompts under the active theme."""

    options = visual_options()
    if not options.color:
        return {
            "qmark": "bold",
            "question": "bold",
            "answer": "bold",
            "pointer": "bold",
            "highlighted": "reverse bold",
            "selected": "bold",
            "disabled": "dim",
            "instruction": "dim",
            "text": "",
        }

    if options.theme == AtlasThemeName.light:
        accent = "#005fbd"
        selected = "#00875f"
        highlight_fg = "#ffffff"
        highlight_bg = accent
        disabled = "#6f6f6f"
        instruction = "#666666"
    elif options.theme == AtlasThemeName.high_contrast:
        accent = "#00ffff"
        selected = "#00ff00"
        highlight_fg = "#000000"
        highlight_bg = "#ffff00"
        disabled = "#ffffff"
        instruction = "#ffffff"
    else:
        accent = "#00d7ff"
        selected = "#00ff87"
        highlight_fg = "#000000"
        highlight_bg = accent
        disabled = "#6c6c6c"
        instruction = "#8a8a8a"

    return {
        "qmark": f"fg:{accent} bold",
        "question": "bold",
        "answer": f"fg:{accent} bold",
        "pointer": f"fg:{accent} bold",
        "highlighted": f"fg:{highlight_fg} bg:{highlight_bg} bold",
        "selected": f"fg:{selected} bold",
        "disabled": f"fg:{disabled}",
        "instruction": f"fg:{instruction}",
        "text": "",
    }


def _coerce_theme(theme: AtlasThemeName | str | None) -> AtlasThemeName:
    if theme is None:
        return AtlasThemeName.auto
    if isinstance(theme, AtlasThemeName):
        return theme
    return AtlasThemeName(theme)


def _env_disables_color(env: Mapping[str, str]) -> bool:
    if env.get("NO_COLOR") is not None:
        return True
    term = env.get("TERM", "").strip().lower()
    return term == "dumb"


def _env_disables_unicode(env: Mapping[str, str]) -> bool:
    term = env.get("TERM", "").strip().lower()
    return term == "dumb"


def _env_disables_motion(env: Mapping[str, str]) -> bool:
    if env.get("ATLAS_NO_ANIMATION") is not None:
        return True
    term = env.get("TERM", "").strip().lower()
    return term == "dumb"


ATLAS_TITLE_STYLE: Final[str] = "atlas.title"
ATLAS_SUBTITLE_STYLE: Final[str] = "atlas.subtitle"
ATLAS_PANEL_STYLE: Final[str] = "atlas.panel"
ATLAS_PATH_STYLE: Final[str] = "atlas.path"
ATLAS_SUCCESS_STYLE: Final[str] = "atlas.success"
ATLAS_WARNING_STYLE: Final[str] = "atlas.warning"
ATLAS_ERROR_STYLE: Final[str] = "atlas.error"
ATLAS_MUTED_STYLE: Final[str] = "atlas.muted"
ATLAS_ACTIVE_STYLE: Final[str] = "atlas.active"
ATLAS_DISABLED_STYLE: Final[str] = "atlas.disabled"
ATLAS_CHOICE_STYLE: Final[str] = "atlas.choice"
ATLAS_PROGRESS_COMPLETE_STYLE: Final[str] = "atlas.progress.complete"
ATLAS_PROGRESS_ACTIVE_STYLE: Final[str] = "atlas.progress.active"
ATLAS_PROGRESS_WAITING_STYLE: Final[str] = "atlas.progress.waiting"
ATLAS_PROGRESS_FILE_STYLE: Final[str] = "atlas.progress.file"
ATLAS_PROGRESS_MEDIA_STYLE: Final[str] = "atlas.progress.media"
ATLAS_PROGRESS_MIRROR_STYLE: Final[str] = "atlas.progress.mirror"
ATLAS_PROGRESS_SHIMMER_STYLE: Final[str] = "atlas.progress.shimmer"


def semantic_bar_text(
    percent: int,
    *,
    width: int = 24,
    style: str = ATLAS_PROGRESS_ACTIVE_STYLE,
    shimmer: bool = True,
    min_nonzero: bool = False,
) -> Text:
    """Render a known-total progress bar using the shared atlas bar grammar."""

    percent = min(100, max(0, percent))
    width = max(0, width)
    filled = round(width * percent / 100)
    if min_nonzero and percent > 0 and filled == 0:
        filled = 1
    text = Text()
    glyphs = bar_glyphs()
    if width <= 0:
        return text
    if filled <= 0:
        text.append(glyphs.empty * width, style=ATLAS_PROGRESS_WAITING_STYLE)
        return text
    if percent >= 100 or not visual_options().motion or not shimmer:
        text.append(glyphs.complete * filled, style=style)
    else:
        frame = int(monotonic() * 4)
        head_width = min(filled, max(1, round(filled * 0.2)))
        if filled >= 4:
            head_width = max(2, head_width)
        head_start = max(0, filled - head_width)
        shimmer_char = glyphs.shimmer[frame % len(glyphs.shimmer)]
        for index in range(filled):
            if index == filled - 1:
                text.append(glyphs.head, style=ATLAS_PROGRESS_SHIMMER_STYLE)
            elif index >= head_start:
                glyph = shimmer_char if (frame + index) % 2 == 0 else glyphs.complete
                text.append(glyph, style=ATLAS_PROGRESS_SHIMMER_STYLE)
            else:
                text.append(glyphs.complete, style=style)
    if filled < width:
        text.append(glyphs.empty * (width - filled), style=ATLAS_PROGRESS_WAITING_STYLE)
    return text


def semantic_pulse_bar_text(
    *,
    width: int = 16,
    style: str = ATLAS_PROGRESS_ACTIVE_STYLE,
    hot_width: int = 6,
) -> Text:
    """Render an unknown-total pulse with active and waiting segments styled separately."""

    width = max(0, width)
    if width <= 0:
        return Text()
    glyphs = bar_glyphs()
    text = Text()
    if not visual_options().motion:
        hot_indexes = set(range(min(hot_width, width)))
        for index in range(width):
            if index in hot_indexes:
                text.append(glyphs.pulse_hot, style=style)
            else:
                text.append(glyphs.pulse, style=ATLAS_PROGRESS_WAITING_STYLE)
        return text

    phase = monotonic() * 1.8
    center = ((width - 1) / 2) + sin(phase * 0.6) * max(1.0, width * 0.16)
    radius = max(1.5, min(width / 2, hot_width / 2 + (sin(phase) + 1.0) * 0.8))
    inner_radius = max(1.0, radius * 0.55)
    for index in range(width):
        distance = abs(index - center)
        if distance <= inner_radius:
            text.append(glyphs.pulse_hot, style=style)
        elif distance <= radius:
            text.append(glyphs.pulse, style=style)
        else:
            text.append(glyphs.pulse, style=ATLAS_PROGRESS_WAITING_STYLE)
    return text
