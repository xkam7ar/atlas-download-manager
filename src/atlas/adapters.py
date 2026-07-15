"""Engine adapter boundary for the atlas command center."""

from __future__ import annotations

from collections.abc import Callable

from atlas.backends import FileDownloadEngine, SiteMirrorEngine
from atlas.config import AtlasSettings
from atlas.engine import YtdlpEngine
from atlas.models import (
    AudioDownloadOptions,
    DownloadResult,
    FileDownloadOptions,
    ProgressEvent,
    SiteDownloadOptions,
    VideoDownloadOptions,
)
from atlas.presets import PostprocessorHook, ProgressHook
from atlas.runner import ProcessControl

FileProgressCallback = Callable[[ProgressEvent], None]


class YtdlpMediaAdapter:
    """Adapter around the embedded yt-dlp Python engine."""

    def __init__(self, settings: AtlasSettings) -> None:
        self._engine = YtdlpEngine(settings=settings)

    def video(
        self,
        options: VideoDownloadOptions,
        *,
        progress_hooks: list[ProgressHook] | None = None,
        postprocessor_hooks: list[PostprocessorHook] | None = None,
    ) -> DownloadResult:
        return self._engine.download_video(
            options,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
        )

    def audio(
        self,
        options: AudioDownloadOptions,
        *,
        progress_hooks: list[ProgressHook] | None = None,
        postprocessor_hooks: list[PostprocessorHook] | None = None,
    ) -> DownloadResult:
        return self._engine.download_audio(
            options,
            progress_hooks=progress_hooks,
            postprocessor_hooks=postprocessor_hooks,
        )


class DirectFileAdapter:
    """Adapter around native, aria2c, and wget2 direct-file backends."""

    def __init__(self) -> None:
        self._engine = FileDownloadEngine()

    def run(
        self,
        options: FileDownloadOptions,
        *,
        progress_callback: FileProgressCallback | None = None,
    ) -> DownloadResult:
        return self._engine.download(options, progress_callback=progress_callback)


class SiteMirrorAdapter:
    """Adapter around wget2/wget website mirror backends."""

    def __init__(self) -> None:
        self._engine = SiteMirrorEngine()

    def run(
        self,
        options: SiteDownloadOptions,
        *,
        progress_callback: FileProgressCallback | None = None,
        control: ProcessControl | None = None,
    ) -> DownloadResult:
        return self._engine.mirror(options, progress_callback=progress_callback, control=control)
