"""Intent routing for the atlas downloader hub."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from pydantic import BaseModel

from atlas.config import AtlasSettings
from atlas.models import EngineKind, EngineRoute, HubKind, HubRequest
from atlas.urls import is_metalink_url

_MEDIA_HOST_MARKERS = (
    "youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
    "music.youtube.com",
    "rumble.com",
)
_FILE_EXTENSIONS = {
    ".7z",
    ".aac",
    ".apk",
    ".avi",
    ".bz2",
    ".dmg",
    ".flac",
    ".gz",
    ".iso",
    ".m4a",
    ".meta4",
    ".metalink",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".pkg",
    ".tar",
    ".tgz",
    ".wav",
    ".webm",
    ".zip",
}


class HubDecision(BaseModel):
    kind: HubKind
    reason: str


def route_url(url: str, requested: HubKind = HubKind.auto) -> HubDecision:
    """Map a user URL and requested intent to a concrete hub action."""

    request = HubRequest(url=url, requested_kind=requested, output_dir=Path.cwd())
    route = EngineRouter().route(request)
    return HubDecision(kind=route.kind, reason=route.reason)


class EngineRouter:
    """Choose the safest concrete download kind and backend for a hub request."""

    def __init__(self, settings: AtlasSettings | None = None) -> None:
        self._settings = settings

    def route(self, request: HubRequest) -> EngineRoute:
        parsed = urlparse(request.url)
        host = (parsed.hostname or "").lower()
        suffix = PurePosixPath(parsed.path).suffix.lower() or None
        is_media_host = _is_media_host(host)

        if request.audio:
            kind = HubKind.audio
            reason = "audio shortcut"
        elif request.requested_kind != HubKind.auto:
            kind = request.requested_kind
            reason = f"user selected {kind.value}"
        elif is_media_host:
            kind = HubKind.video
            reason = "media host"
        elif is_metalink_url(request.url):
            kind = HubKind.manifest
            reason = f"metalink manifest {suffix}"
        elif suffix in _FILE_EXTENSIONS:
            kind = HubKind.file
            reason = f"file extension {suffix}"
        else:
            kind = HubKind.file
            reason = "safe default for non-media URL"

        return EngineRoute(
            kind=kind,
            engine=self._engine_for(kind, request.backend),
            reason=reason,
            url=request.url,
            output_dir=request.output_dir,
            is_media_host=is_media_host,
            file_suffix=suffix,
            safety=self._safety_notes(kind, request),
        )

    def _engine_for(self, kind: HubKind, backend: str) -> EngineKind:
        if kind in {HubKind.video, HubKind.audio}:
            return EngineKind.ytdlp
        if kind in {HubKind.site, HubKind.dir}:
            if backend == "wget":
                return EngineKind.wget
            return EngineKind.wget2
        if kind == HubKind.manifest:
            return EngineKind.aria2
        if backend == "native":
            return EngineKind.native
        if backend == "aria2":
            return EngineKind.aria2
        if backend == "wget2":
            return EngineKind.wget2
        if self._settings and not self._settings.aria2:
            return EngineKind.native
        return EngineKind.aria2

    def _safety_notes(self, kind: HubKind, request: HubRequest) -> list[str]:
        notes: list[str] = []
        if kind in {HubKind.video, HubKind.audio}:
            notes.append("single item unless explicit playlist command/options are used")
            notes.append("archive enabled when configured")
        elif kind == HubKind.site:
            notes.append("recursive mirroring is explicit")
            notes.append("host spanning is disabled unless requested")
        elif kind == HubKind.dir:
            notes.append("open-directory mirroring is explicit")
            notes.append("host spanning and parent ascent are disabled unless requested")
        elif kind == HubKind.manifest:
            notes.append("Metalink expansion uses aria2c")
            notes.append("manifest file is not saved unless Metalink mode is disabled")
        else:
            notes.append("resume enabled by default")
            notes.append("native fallback available if aria2c is unavailable")
        if request.dry_run:
            notes.append("dry run: no network request or download")
        return notes


def _is_media_host(host: str) -> bool:
    return any(host == marker or host.endswith(f".{marker}") for marker in _MEDIA_HOST_MARKERS)
