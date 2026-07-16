"""Probe-driven media capability resolution.

The menu uses this layer after yt-dlp probes a URL and before it asks the user
for media choices.  It turns the raw format list into safe, source-aware
profiles so normal users do not have to guess codec/container combinations.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from atlas.models import (
    AudioCodec,
    AudioDownloadOptions,
    Container,
    FormatInfo,
    MediaInfo,
    QualityIntent,
    ResolutionChoice,
    VideoCodecChoice,
    VideoDownloadOptions,
)
from atlas.theme import visual_join


class MediaProfile(StrEnum):
    best = "best"
    balanced = "balanced"
    compatible = "compatible"
    small = "small"
    audio_best = "audio_best"
    audio_mp3 = "audio_mp3"
    custom = "custom"


class CapabilityStatus(StrEnum):
    available = "available"
    fallback = "fallback"
    requires_remux = "requires_remux"
    requires_transcode = "requires_transcode"
    unavailable = "unavailable"


@dataclass(frozen=True)
class MediaChoice:
    profile: MediaProfile
    label: str
    status: CapabilityStatus
    format_selector: str
    format_sort: tuple[str, ...] = ()
    container: str = "auto"
    video_codec: str | None = None
    audio_codec: str | None = None
    resolution: str | None = None
    estimated_size: int | None = None
    warnings: tuple[str, ...] = ()
    requires_ffmpeg: bool = False
    requires_transcode: bool = False
    video_format_id: str | None = None
    audio_format_id: str | None = None

    @property
    def selectable(self) -> bool:
        return self.status != CapabilityStatus.unavailable


@dataclass(frozen=True)
class MediaCapabilityCatalog:
    info: MediaInfo
    formats: tuple[FormatInfo, ...]
    video_formats: tuple[FormatInfo, ...] = field(default_factory=tuple)
    audio_formats: tuple[FormatInfo, ...] = field(default_factory=tuple)
    combined_formats: tuple[FormatInfo, ...] = field(default_factory=tuple)
    is_audio_only: bool = False
    is_live: bool = False

    @classmethod
    def from_media_info(cls, info: MediaInfo) -> MediaCapabilityCatalog:
        formats = tuple(info.formats)
        videos = tuple(fmt for fmt in formats if _has_video(fmt))
        audios = tuple(fmt for fmt in formats if _is_audio_only(fmt))
        combined = tuple(fmt for fmt in formats if _has_video(fmt) and _has_audio(fmt))
        return cls(
            info=info,
            formats=formats,
            video_formats=tuple(sorted(videos, key=_video_sort_key, reverse=True)),
            audio_formats=tuple(sorted(audios, key=_audio_sort_key, reverse=True)),
            combined_formats=tuple(sorted(combined, key=_video_sort_key, reverse=True)),
            is_audio_only=not videos and bool(audios),
            is_live=any((fmt.protocol or "").lower() in {"m3u8", "m3u8_native"} for fmt in formats),
        )


class MediaCapabilityResolver:
    """Generate safe profile choices from probed media formats."""

    def __init__(self, catalog: MediaCapabilityCatalog) -> None:
        self.catalog = catalog

    @classmethod
    def from_info(cls, info: MediaInfo) -> MediaCapabilityResolver:
        return cls(MediaCapabilityCatalog.from_media_info(info))

    def video_profiles(self) -> list[MediaChoice]:
        choices = [
            self._best_video_choice(),
            self._balanced_choice(),
            self._compatible_choice(),
            self._small_choice(),
            self._audio_best_choice(),
        ]
        return [choice for choice in choices if choice.selectable]

    def audio_profiles(self) -> list[MediaChoice]:
        choices = [
            self._audio_best_choice(),
            self._audio_mp3_choice(),
        ]
        return [choice for choice in choices if choice.selectable]

    def all_profiles(self) -> list[MediaChoice]:
        seen: set[MediaProfile] = set()
        ordered: list[MediaChoice] = []
        for choice in [*self.video_profiles(), *self.audio_profiles()]:
            if choice.profile in seen:
                continue
            seen.add(choice.profile)
            ordered.append(choice)
        return ordered

    def apply_video_choice(
        self,
        options: VideoDownloadOptions,
        choice: MediaChoice,
    ) -> VideoDownloadOptions:
        update: dict[str, object] = {
            "format": choice.format_selector,
            "container": _container_choice(choice.container),
            "quality": _quality_for_profile(choice.profile),
            "video_codec": _video_codec_choice(choice.video_codec),
        }
        resolution = _resolution_choice(choice.resolution)
        if resolution is not None:
            update["resolution"] = resolution
        return options.model_copy(update=update)

    def apply_audio_choice(
        self,
        options: AudioDownloadOptions,
        choice: MediaChoice,
    ) -> AudioDownloadOptions:
        codec = AudioCodec.mp3 if choice.profile == MediaProfile.audio_mp3 else AudioCodec.best
        return options.model_copy(update={"format": choice.format_selector, "codec": codec})

    def choice_for_exact_formats(
        self,
        video: FormatInfo,
        audio: FormatInfo | None,
    ) -> MediaChoice:
        return _video_choice(
            MediaProfile.custom,
            "Custom formats",
            video,
            audio,
            CapabilityStatus.available,
        )

    def choice_for_exact_audio(self, audio: FormatInfo) -> MediaChoice:
        return MediaChoice(
            profile=MediaProfile.custom,
            label="Custom audio",
            status=CapabilityStatus.available,
            format_selector=audio.format_id,
            container=audio.ext or "auto",
            audio_codec=_codec_family(audio.acodec),
            estimated_size=audio.filesize,
            audio_format_id=audio.format_id,
        )

    def _best_video_choice(self) -> MediaChoice:
        video = _first(self.catalog.video_formats)
        if video is None:
            return _unavailable(MediaProfile.best, "Best quality")
        audio = _first(self.catalog.audio_formats)
        return _video_choice(MediaProfile.best, "Best quality", video, audio)

    def _balanced_choice(self) -> MediaChoice:
        video = _best_under_height(self.catalog.video_formats, 1440) or _first(
            self.catalog.video_formats
        )
        if video is None:
            return _unavailable(MediaProfile.balanced, "Balanced")
        status = (
            CapabilityStatus.available
            if _height(video.resolution) <= 1440 or _height(video.resolution) == 0
            else CapabilityStatus.fallback
        )
        warnings: tuple[str, ...] = ()
        if status == CapabilityStatus.fallback:
            warnings = ("No 1440p-or-lower stream was found; using best available source.",)
        return _video_choice(
            MediaProfile.balanced,
            "Balanced",
            video,
            _first(self.catalog.audio_formats),
            status,
            warnings=warnings,
        )

    def _small_choice(self) -> MediaChoice:
        candidates = list(_formats_under_height(self.catalog.video_formats, 720))
        if not candidates:
            video = _first(self.catalog.video_formats)
            if video is None:
                return _unavailable(MediaProfile.small, "Small file")
            warnings: tuple[str, ...] = (
                "No 720p-or-lower stream was found; using the smallest available video.",
            )
            status = CapabilityStatus.fallback
        else:
            video = min(candidates, key=_small_sort_key)
            warnings = ()
            status = CapabilityStatus.available
        return _video_choice(
            MediaProfile.small,
            "Small file",
            video,
            _first(self.catalog.audio_formats),
            status,
            warnings=warnings,
        )

    def _compatible_choice(self) -> MediaChoice:
        m4a_audio = _best_m4a_audio(self.catalog.audio_formats)
        audio = m4a_audio or _first(self.catalog.audio_formats)
        exact = _best_compatible_video(self.catalog.video_formats)
        if exact is not None:
            status = CapabilityStatus.available
            warnings: tuple[str, ...] = ()
            requires_ffmpeg = False
            requires_transcode = False
            if audio is not None and m4a_audio is None:
                status = CapabilityStatus.requires_transcode
                warnings = ("This profile needs ffmpeg to create MP4 output.",)
                requires_ffmpeg = True
                requires_transcode = True
            return _video_choice(
                MediaProfile.compatible,
                "Apple compatible",
                exact,
                audio,
                status,
                container="mp4",
                audio_codec="AAC" if requires_transcode else None,
                format_sort=("codec:h264", "ext:mp4:m4a", "res", "fps"),
                warnings=warnings,
                requires_ffmpeg=requires_ffmpeg,
                requires_transcode=requires_transcode,
            )
        mp4 = _best_mp4_video(self.catalog.video_formats)
        if mp4 is not None:
            return _video_choice(
                MediaProfile.compatible,
                "Apple compatible",
                mp4,
                audio,
                CapabilityStatus.fallback,
                container="mp4",
                format_sort=("ext:mp4:m4a", "res", "fps"),
                warnings=("No H.264 or HEVC MP4 stream was found; using the nearest MP4 source.",),
            )
        source = _best_under_height(self.catalog.video_formats, 1080) or _first(
            self.catalog.video_formats
        )
        if source is None:
            return _unavailable(MediaProfile.compatible, "Apple compatible")
        return _video_choice(
            MediaProfile.compatible,
            "Apple compatible",
            source,
            audio,
            CapabilityStatus.requires_transcode,
            container="mp4",
            video_codec="H.264",
            audio_codec="AAC",
            warnings=(
                "This source has no MP4/H.264-compatible stream; re-encode requires ffmpeg.",
            ),
            requires_ffmpeg=True,
            requires_transcode=True,
        )

    def _audio_best_choice(self) -> MediaChoice:
        audio = _first(self.catalog.audio_formats)
        if audio is not None:
            return MediaChoice(
                profile=MediaProfile.audio_best,
                label="Audio only",
                status=CapabilityStatus.available,
                format_selector=audio.format_id,
                container=audio.ext or "auto",
                audio_codec=_codec_family(audio.acodec),
                estimated_size=audio.filesize,
                audio_format_id=audio.format_id,
            )
        combined = _first(self.catalog.combined_formats)
        if combined is None:
            return _unavailable(MediaProfile.audio_best, "Audio only")
        return MediaChoice(
            profile=MediaProfile.audio_best,
            label="Audio only",
            status=CapabilityStatus.fallback,
            format_selector=combined.format_id,
            container=combined.ext or "auto",
            audio_codec=_codec_family(combined.acodec),
            estimated_size=combined.filesize,
            warnings=("No separate audio stream was found; using the best combined source.",),
            audio_format_id=combined.format_id,
        )

    def _audio_mp3_choice(self) -> MediaChoice:
        base = self._audio_best_choice()
        if not base.selectable:
            return _unavailable(MediaProfile.audio_mp3, "MP3")
        return MediaChoice(
            profile=MediaProfile.audio_mp3,
            label="MP3",
            status=CapabilityStatus.requires_transcode,
            format_selector=base.format_selector,
            container="mp3",
            audio_codec="MP3",
            estimated_size=base.estimated_size,
            warnings=("MP3 output uses ffmpeg.",),
            requires_ffmpeg=True,
            requires_transcode=True,
            audio_format_id=base.audio_format_id,
        )


def format_choice_label(choice: MediaChoice) -> str:
    parts = [choice.label]
    details: list[str] = []
    if choice.resolution:
        details.append(choice.resolution)
    codecs = " + ".join(
        value for value in (choice.video_codec, choice.audio_codec) if value and value != "none"
    )
    if codecs:
        details.append(codecs)
    if choice.container and choice.container != "auto":
        container = _display_container(choice.container)
        if container not in details:
            details.append(container)
    if details:
        parts.append("  " + visual_join(details))
    return "".join(parts)


def _display_container(value: str) -> str:
    labels = {
        "m4a": "M4A",
        "mkv": "MKV",
        "mp3": "MP3",
        "mp4": "MP4",
        "webm": "WebM",
    }
    return labels.get(value.lower(), value.upper())


def format_format_row(fmt: FormatInfo) -> str:
    parts = [
        fmt.format_id,
        _resolution_label(fmt.resolution),
        _codec_family(fmt.vcodec) if _has_video(fmt) else _codec_family(fmt.acodec),
        fmt.ext or "-",
    ]
    if fmt.fps:
        parts.append(f"{fmt.fps:g}fps")
    if fmt.tbr:
        parts.append(f"{fmt.tbr:g}k")
    if fmt.filesize:
        parts.append(_bytes(fmt.filesize))
    if fmt.note:
        parts.append(fmt.note)
    return "  ".join(part for part in parts if part and part != "-")


def _video_choice(
    profile: MediaProfile,
    label: str,
    video: FormatInfo,
    audio: FormatInfo | None,
    status: CapabilityStatus = CapabilityStatus.available,
    *,
    container: str | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    format_sort: Sequence[str] = (),
    warnings: Sequence[str] = (),
    requires_ffmpeg: bool = False,
    requires_transcode: bool = False,
) -> MediaChoice:
    return MediaChoice(
        profile=profile,
        label=label,
        status=status,
        format_selector=_format_selector(video, audio),
        format_sort=tuple(format_sort),
        container=container or _container_for(video, audio),
        video_codec=video_codec or _codec_family(video.vcodec),
        audio_codec=audio_codec
        or (_codec_family(audio.acodec) if audio else _codec_family(video.acodec)),
        resolution=_resolution_label(video.resolution),
        estimated_size=_combined_filesize(video, audio),
        warnings=tuple(warnings),
        requires_ffmpeg=requires_ffmpeg,
        requires_transcode=requires_transcode,
        video_format_id=video.format_id,
        audio_format_id=audio.format_id if audio and not _has_audio(video) else None,
    )


def _unavailable(profile: MediaProfile, label: str) -> MediaChoice:
    return MediaChoice(
        profile=profile,
        label=label,
        status=CapabilityStatus.unavailable,
        format_selector="",
    )


def _first[T](items: Sequence[T]) -> T | None:
    return items[0] if items else None


def _has_video(fmt: FormatInfo) -> bool:
    return bool(fmt.vcodec and fmt.vcodec != "none")


def _has_audio(fmt: FormatInfo) -> bool:
    return bool(fmt.acodec and fmt.acodec != "none")


def _is_audio_only(fmt: FormatInfo) -> bool:
    return _has_audio(fmt) and not _has_video(fmt)


def _height(resolution: str | None) -> int:
    if not resolution:
        return 0
    tail = resolution.rsplit("x", 1)[-1] if "x" in resolution else resolution.rstrip("p")
    try:
        return int(tail)
    except ValueError:
        return 0


def _resolution_label(value: str | None) -> str | None:
    if not value:
        return None
    if value == "audio only":
        return "audio"
    if "x" in value:
        height = value.rsplit("x", 1)[-1]
        return f"{height}p" if height.isdigit() else value
    return value


def _codec_family(value: str | None) -> str:
    if not value or value == "none":
        return "none"
    lowered = value.lower()
    if lowered.startswith("av01"):
        return "AV1"
    if lowered.startswith(("vp09", "vp9")):
        return "VP9"
    if lowered.startswith("avc1"):
        return "H.264"
    if lowered.startswith(("hvc1", "hev1")):
        return "HEVC"
    if lowered.startswith("mp4a"):
        return "AAC"
    if lowered.startswith("opus"):
        return "Opus"
    return value.split(".", 1)[0]


def _codec_rank(value: str | None) -> int:
    return {"AV1": 5, "VP9": 4, "HEVC": 3, "H.264": 2}.get(_codec_family(value), 1)


def _video_sort_key(fmt: FormatInfo) -> tuple[int, float, int, float, int]:
    return (
        _height(fmt.resolution),
        fmt.fps or 0.0,
        _codec_rank(fmt.vcodec),
        fmt.tbr or 0.0,
        fmt.filesize or 0,
    )


def _audio_sort_key(fmt: FormatInfo) -> tuple[float, int]:
    return (fmt.tbr or 0.0, fmt.filesize or 0)


def _small_sort_key(fmt: FormatInfo) -> tuple[int, float]:
    return (fmt.filesize or 10**18, fmt.tbr or 10**18)


def _formats_under_height(formats: Iterable[FormatInfo], height: int) -> list[FormatInfo]:
    return [fmt for fmt in formats if 0 < _height(fmt.resolution) <= height]


def _best_under_height(formats: Sequence[FormatInfo], height: int) -> FormatInfo | None:
    matches = _formats_under_height(formats, height)
    return max(matches, key=_video_sort_key) if matches else None


def _best_compatible_video(formats: Sequence[FormatInfo]) -> FormatInfo | None:
    candidates = [
        fmt
        for fmt in formats
        if fmt.ext == "mp4"
        and 0 < _height(fmt.resolution) <= 1080
        and _codec_family(fmt.vcodec) in {"H.264", "HEVC"}
    ]
    return max(candidates, key=_video_sort_key) if candidates else None


def _best_mp4_video(formats: Sequence[FormatInfo]) -> FormatInfo | None:
    candidates = [fmt for fmt in formats if fmt.ext == "mp4" and _height(fmt.resolution) <= 1080]
    return max(candidates, key=_video_sort_key) if candidates else None


def _best_m4a_audio(formats: Sequence[FormatInfo]) -> FormatInfo | None:
    candidates = [fmt for fmt in formats if fmt.ext in {"m4a", "mp4"}]
    return max(candidates, key=_audio_sort_key) if candidates else None


def _container_for(video: FormatInfo, audio: FormatInfo | None) -> str:
    video_ext = video.ext or "mkv"
    audio_ext = audio.ext if audio else None
    if not audio_ext or audio_ext == "none" or _has_audio(video):
        return video_ext
    if video_ext == "mp4" and audio_ext in {"m4a", "mp4"}:
        return "mp4"
    if video_ext == "webm" and audio_ext == "webm":
        return "webm"
    return "mkv"


def _format_selector(video: FormatInfo, audio: FormatInfo | None) -> str:
    if audio is None or _has_audio(video):
        return video.format_id
    return f"{video.format_id}+{audio.format_id}"


def _combined_filesize(video: FormatInfo, audio: FormatInfo | None) -> int | None:
    if video.filesize is None:
        return None
    if audio is None or audio.filesize is None or _has_audio(video):
        return video.filesize
    return video.filesize + audio.filesize


def _container_choice(value: str) -> Container:
    return {
        "mkv": Container.mkv,
        "mp4": Container.mp4,
        "webm": Container.webm,
    }.get(value, Container.auto)


def _quality_for_profile(profile: MediaProfile) -> QualityIntent:
    if profile == MediaProfile.balanced:
        return QualityIntent.balanced
    if profile == MediaProfile.compatible:
        return QualityIntent.compatible
    if profile == MediaProfile.small:
        return QualityIntent.small
    return QualityIntent.max


def _video_codec_choice(value: str | None) -> VideoCodecChoice:
    return {
        "AV1": VideoCodecChoice.av1,
        "VP9": VideoCodecChoice.vp9,
        "H.264": VideoCodecChoice.h264,
        "HEVC": VideoCodecChoice.hevc,
    }.get(value or "", VideoCodecChoice.auto)


def _resolution_choice(value: str | None) -> ResolutionChoice | None:
    if not value or not value.endswith("p"):
        return None
    return {
        "4320p": ResolutionChoice.r4320,
        "2160p": ResolutionChoice.r2160,
        "1440p": ResolutionChoice.r1440,
        "1080p": ResolutionChoice.r1080,
        "720p": ResolutionChoice.r720,
        "480p": ResolutionChoice.r480,
    }.get(value)


def _bytes(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or suffix == "TB":
            return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"
