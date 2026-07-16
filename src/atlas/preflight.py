"""Pre-download dependency checks."""

from __future__ import annotations

from shutil import which

from atlas.config import AtlasSettings
from atlas.errors import DependencyMissingError
from atlas.models import BatchKind, DownloadPlan
from atlas.setup import install_hint_for_tool


def ensure_download_dependencies(
    settings: AtlasSettings,
    kind: BatchKind,
    plan: DownloadPlan,
) -> None:
    """Fail early when required local tools are unavailable."""

    _ = settings, plan
    missing = [tool for tool in ("ffmpeg", "ffprobe") if which(tool) is None]
    if not missing:
        return

    tools = " and ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    action = "audio extraction" if kind == BatchKind.audio else "video merging"
    install = "Install it with" if len(missing) == 1 else "Install them with"
    raise DependencyMissingError(
        f"{tools} {verb} required for {action}. {install}: {install_hint_for_tool('ffmpeg')}"
    )
