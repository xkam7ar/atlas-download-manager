"""Information sanitization and format modeling."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from atlas.models import FormatInfo, FormatSort, MediaFormatChoice, MediaInfo
from atlas.redaction import redact_url, sanitize_terminal_text

_SAFE_INFO_FIELDS = {
    "id",
    "title",
    "uploader",
    "channel",
    "duration",
    "webpage_url",
    "extractor",
    "upload_date",
    "view_count",
    "availability",
    "_type",
    "playlist_count",
    "formats",
    "entries",
}


def sanitize_info(info: dict[str, Any]) -> dict[str, Any]:
    """Keep display-safe metadata and omit cookies, headers, and large raw fields."""

    sanitized = {key: info.get(key) for key in _SAFE_INFO_FIELDS if key in info}
    webpage_url = sanitized.get("webpage_url")
    if isinstance(webpage_url, str):
        sanitized["webpage_url"] = sanitize_terminal_text(redact_url(webpage_url))
    for field in ("id", "title", "uploader", "channel", "extractor", "availability"):
        value = sanitized.get(field)
        if isinstance(value, str):
            sanitized[field] = sanitize_terminal_text(value)
    if "formats" in sanitized and isinstance(sanitized["formats"], list):
        sanitized["formats"] = [
            {
                key: (
                    sanitize_terminal_text(value)
                    if isinstance((value := fmt.get(key)), str)
                    else value
                )
                for key in (
                    "format_id",
                    "ext",
                    "resolution",
                    "width",
                    "height",
                    "fps",
                    "vcodec",
                    "acodec",
                    "filesize",
                    "filesize_approx",
                    "tbr",
                    "abr",
                    "protocol",
                    "format_note",
                )
                if key in fmt
            }
            for fmt in sanitized["formats"]
            if isinstance(fmt, dict)
        ]
    if "entries" in sanitized and isinstance(sanitized["entries"], list):
        sanitized["playlist_count"] = sanitized.get("playlist_count") or len(sanitized["entries"])
        sanitized.pop("entries", None)
    return sanitized


def _resolution(fmt: dict[str, Any]) -> str | None:
    resolution = fmt.get("resolution")
    if resolution and resolution != "audio only":
        return str(resolution)
    width = fmt.get("width")
    height = fmt.get("height")
    if width and height:
        return f"{width}x{height}"
    if fmt.get("vcodec") == "none":
        return "audio only"
    return resolution


def formats_from_info(info: dict[str, Any]) -> list[FormatInfo]:
    formats: list[FormatInfo] = []
    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        formats.append(
            FormatInfo(
                format_id=str(fmt.get("format_id") or "?"),
                ext=fmt.get("ext"),
                resolution=_resolution(fmt),
                fps=fmt.get("fps"),
                vcodec=fmt.get("vcodec"),
                acodec=fmt.get("acodec"),
                filesize=filesize,
                tbr=fmt.get("tbr") or fmt.get("abr"),
                protocol=fmt.get("protocol"),
                note=fmt.get("format_note"),
            )
        )
    return formats


def _best_video(formats: list[FormatInfo]) -> str | None:
    video_formats = [fmt for fmt in formats if fmt.vcodec and fmt.vcodec != "none"]
    if not video_formats:
        return None
    best = max(video_formats, key=_best_video_sort_key)
    return f"{best.format_id} {best.ext or ''} {best.resolution or ''} {best.vcodec or ''}".strip()


def _best_audio(formats: list[FormatInfo]) -> str | None:
    audio_formats = [
        fmt for fmt in formats if fmt.acodec and fmt.acodec != "none" and fmt.vcodec == "none"
    ]
    if not audio_formats:
        return None
    best = max(audio_formats, key=lambda fmt: (fmt.tbr or 0, fmt.filesize or 0))
    return f"{best.format_id} {best.ext or ''} {best.tbr or 0:g}k {best.acodec or ''}".strip()


def _height(resolution: str | None) -> int:
    if not resolution:
        return 0
    tail = resolution.rsplit("x", 1)[-1] if "x" in resolution else resolution.rstrip("p")
    try:
        return int(tail)
    except ValueError:
        return 0


def _resolution_label(value: str | None) -> str:
    if not value:
        return "-"
    if value == "audio only":
        return "audio"
    if "x" in value:
        height = value.rsplit("x", 1)[-1]
        return f"{height}p" if height.isdigit() else value
    return value


def _codec_family(value: str | None) -> str:
    if not value or value == "none":
        return "unknown"
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
    return {
        "AV1": 5,
        "VP9": 4,
        "HEVC": 3,
        "H.264": 2,
    }.get(_codec_family(value), 1)


def _best_video_sort_key(fmt: FormatInfo) -> tuple[int, float, int, float, int]:
    return (
        _height(fmt.resolution),
        fmt.fps or 0.0,
        _codec_rank(fmt.vcodec),
        fmt.tbr or 0.0,
        fmt.filesize or 0,
    )


def _best_audio_sort_key(fmt: FormatInfo) -> tuple[float, int]:
    return (fmt.tbr or 0.0, fmt.filesize or 0)


def _container_for(video: FormatInfo, audio: FormatInfo | None) -> str:
    video_ext = video.ext or "mkv"
    audio_ext = audio.ext if audio else None
    if not audio_ext or audio_ext == "none":
        return video_ext
    if video_ext == "mp4" and audio_ext in {"m4a", "mp4"}:
        return "mp4"
    if video_ext == "webm" and audio_ext == "webm":
        return "webm"
    return "mkv"


def _choice_format(video: FormatInfo, audio: FormatInfo | None) -> str:
    if audio is None:
        return video.format_id
    if video.acodec and video.acodec != "none":
        return video.format_id
    return f"{video.format_id}+{audio.format_id}"


def _choice_filesize(video: FormatInfo, audio: FormatInfo | None) -> int | None:
    if video.filesize is None:
        return None
    if audio is None or audio.filesize is None or video.acodec != "none":
        return video.filesize
    return video.filesize + audio.filesize


def _media_choice(label: str, video: FormatInfo, audio: FormatInfo | None) -> MediaFormatChoice:
    return MediaFormatChoice(
        label=label,
        format=_choice_format(video, audio),
        container=_container_for(video, audio),
        resolution=_resolution_label(video.resolution),
        video_codec=_codec_family(video.vcodec),
        audio_codec=_codec_family(audio.acodec) if audio else _codec_family(video.acodec),
        video_format_id=video.format_id,
        audio_format_id=audio.format_id if audio and video.acodec == "none" else None,
        filesize=_choice_filesize(video, audio),
        note=video.note,
    )


def best_media_choices(
    formats: Sequence[FormatInfo],
    *,
    limit: int = 6,
) -> list[MediaFormatChoice]:
    """Return intent-level best video+audio choices grouped by codec family."""

    videos = [fmt for fmt in formats if fmt.vcodec and fmt.vcodec != "none"]
    audios = [
        fmt for fmt in formats if fmt.acodec and fmt.acodec != "none" and fmt.vcodec == "none"
    ]
    if not videos:
        return []
    best_audio = max(audios, key=_best_audio_sort_key) if audios else None
    best_overall = max(videos, key=_best_video_sort_key)
    choices = [_media_choice("Max quality", best_overall, best_audio)]
    best_by_codec: dict[str, FormatInfo] = {}
    for video in videos:
        family = _codec_family(video.vcodec)
        current = best_by_codec.get(family)
        if current is None or _best_video_sort_key(video) > _best_video_sort_key(current):
            best_by_codec[family] = video
    preferred_order = ["AV1", "VP9", "HEVC", "H.264"]
    ordered_codecs = [
        *[codec for codec in preferred_order if codec in best_by_codec],
        *sorted(codec for codec in best_by_codec if codec not in preferred_order),
    ]
    for codec in ordered_codecs:
        choice = _media_choice(f"Best {codec}", best_by_codec[codec], best_audio)
        choices.append(choice)
        if len(choices) >= limit:
            break
    return choices


def media_info_from_raw(info: dict[str, Any]) -> MediaInfo:
    sanitized = sanitize_info(info)
    formats = formats_from_info(sanitized)
    is_playlist = sanitized.get("_type") == "playlist" or bool(sanitized.get("playlist_count"))
    return MediaInfo(
        id=sanitized.get("id"),
        title=sanitized.get("title"),
        uploader=sanitized.get("uploader"),
        channel=sanitized.get("channel"),
        duration=sanitized.get("duration"),
        webpage_url=sanitized.get("webpage_url"),
        extractor=sanitized.get("extractor"),
        upload_date=sanitized.get("upload_date"),
        view_count=sanitized.get("view_count"),
        availability=sanitized.get("availability"),
        is_playlist=is_playlist,
        playlist_count=sanitized.get("playlist_count"),
        best_video=_best_video(formats),
        best_audio=_best_audio(formats),
        formats=formats,
    )


def filter_formats(
    formats: Sequence[FormatInfo],
    *,
    video_only: bool = False,
    audio_only: bool = False,
) -> list[FormatInfo]:
    selected = list(formats)
    if video_only:
        selected = [fmt for fmt in selected if fmt.vcodec and fmt.vcodec != "none"]
    if audio_only:
        selected = [
            fmt for fmt in selected if fmt.acodec and fmt.acodec != "none" and fmt.vcodec == "none"
        ]
    return selected


def sort_formats(formats: list[FormatInfo], sort: FormatSort) -> list[FormatInfo]:
    if sort == FormatSort.size:
        return sorted(formats, key=lambda fmt: (fmt.filesize or 0, fmt.tbr or 0), reverse=True)
    if sort == FormatSort.codec:
        return sorted(formats, key=lambda fmt: (fmt.vcodec or "", fmt.acodec or "", fmt.ext or ""))
    return sorted(
        formats,
        key=_format_quality_sort_key,
        reverse=True,
    )


def _format_quality_sort_key(fmt: FormatInfo) -> tuple[int, int, float, int, float, int]:
    if fmt.vcodec and fmt.vcodec != "none":
        return (1, *_best_video_sort_key(fmt))
    return (0, 0, 0.0, 0, fmt.tbr or 0.0, fmt.filesize or 0)


def format_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or suffix == "TB":
            return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
