"""UI-free progress event normalization for backend callbacks."""

from __future__ import annotations

import re
from typing import Any

from atlas.models import EngineKind, HubKind, ProgressEvent, ProgressPhase

_ARIA2_BYTES_PATTERN = re.compile(
    r"\[#\w+\s+"
    r"(?P<downloaded>[0-9.]+)(?P<downloaded_unit>[KMGTP]?i?B)"
    r"(?:/"
    r"(?P<total>[0-9.]+)(?P<total_unit>[KMGTP]?i?B)"
    r")?"
)
_ARIA2_SPEED_PATTERN = re.compile(r"\bDL:(?P<speed>[0-9.]+)(?P<speed_unit>[KMGTP]?i?B)")
_ARIA2_ETA_PATTERN = re.compile(r"\bETA:(?P<eta>[^\]\s]+)")
_WGET_PERCENT_PATTERN = re.compile(r"(?P<percent>\d{1,3})%")
_BYTE_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def progress_event_from_ytdlp(
    raw_event: dict[str, Any],
    *,
    line_no: int | None = None,
    url: str | None = None,
    kind: HubKind | None = None,
) -> ProgressEvent:
    """Convert a raw yt-dlp progress hook payload into a neutral event."""

    info = raw_event.get("info_dict") or {}
    title = str(info.get("title") or "")
    filename = raw_event.get("filename")
    return ProgressEvent(
        engine=EngineKind.ytdlp,
        status=str(raw_event.get("status") or "unknown"),
        phase=ProgressPhase.download,
        kind=kind,
        filename=filename if isinstance(filename, str) else None,
        title=title or None,
        url=url or _optional_str(info.get("webpage_url")),
        item_id=str(line_no) if line_no is not None else _optional_str(info.get("id")),
        line_no=line_no,
        downloaded_bytes=_optional_int(raw_event.get("downloaded_bytes")),
        total_bytes=_optional_int(raw_event.get("total_bytes"))
        or _optional_int(raw_event.get("total_bytes_estimate")),
        fragment_index=_optional_int(raw_event.get("fragment_index")),
        fragment_count=_optional_int(raw_event.get("fragment_count")),
        speed_bytes_per_sec=_optional_float(raw_event.get("speed")),
        eta_seconds=_optional_float(raw_event.get("eta")),
    )


def progress_event_from_ytdlp_postprocessor(
    raw_event: dict[str, Any],
    *,
    line_no: int | None = None,
    url: str | None = None,
    kind: HubKind | None = None,
) -> ProgressEvent:
    """Convert a raw yt-dlp postprocessor hook payload into a neutral event."""

    info = raw_event.get("info_dict") or {}
    postprocessor = _optional_str(raw_event.get("postprocessor")) or "Postprocessor"
    status = _postprocessor_status(raw_event.get("status"))
    filename = _optional_str(info.get("filepath")) or _optional_str(raw_event.get("filename"))
    return ProgressEvent(
        engine=EngineKind.ytdlp,
        status=status,
        phase=_postprocessor_phase(postprocessor),
        kind=kind,
        filename=filename,
        title=_optional_str(info.get("title")),
        url=url or _optional_str(info.get("webpage_url")),
        item_id=str(line_no) if line_no is not None else _optional_str(info.get("id")),
        line_no=line_no,
        message=_postprocessor_message(postprocessor, status),
    )


def progress_event_from_aria2_line(
    line: str,
    *,
    filename: str | None = None,
    url: str | None = None,
    kind: HubKind | None = HubKind.file,
) -> ProgressEvent | None:
    """Parse an aria2c console progress line into a neutral event when possible."""

    match = _ARIA2_BYTES_PATTERN.search(line)
    if not match:
        return None
    speed_match = _ARIA2_SPEED_PATTERN.search(line)
    eta_match = _ARIA2_ETA_PATTERN.search(line)
    downloaded = _parse_size(match.group("downloaded"), match.group("downloaded_unit"))
    total = _parse_size(match.group("total"), match.group("total_unit"))
    speed = (
        _parse_size(speed_match.group("speed"), speed_match.group("speed_unit"))
        if speed_match
        else None
    )
    return ProgressEvent(
        engine=EngineKind.aria2,
        status="downloading",
        phase=ProgressPhase.download,
        kind=kind,
        filename=filename,
        url=url,
        downloaded_bytes=downloaded,
        total_bytes=total,
        speed_bytes_per_sec=float(speed) if speed is not None else None,
        eta_seconds=_parse_eta(eta_match.group("eta")) if eta_match else None,
    )


def progress_event_from_wget2_line(
    line: str,
    *,
    filename: str | None = None,
    url: str | None = None,
    kind: HubKind | None = HubKind.site,
) -> ProgressEvent | None:
    """Parse a coarse wget2/wget output line into a neutral event when possible."""

    if not line.strip():
        return None
    percent_match = _WGET_PERCENT_PATTERN.search(line)
    status = "downloading" if percent_match else "phase"
    return ProgressEvent(
        engine=EngineKind.wget2,
        status=status,
        phase=ProgressPhase.download if status == "downloading" else ProgressPhase.extract,
        kind=kind,
        filename=filename,
        url=url,
        percent=float(percent_match.group("percent")) if percent_match else None,
        message=" ".join(line.split()),
    )


def _postprocessor_status(raw_status: object) -> str:
    if raw_status == "finished":
        return "done"
    if raw_status in {"started", "processing"}:
        return "running"
    if raw_status == "error":
        return "error"
    return str(raw_status or "running")


def _postprocessor_phase(postprocessor: str) -> ProgressPhase:
    name = postprocessor.lower()
    if "merger" in name or "concat" in name:
        return ProgressPhase.merge
    if "extractaudio" in name:
        return ProgressPhase.extract
    if "movefilesafterdownload" in name:
        return ProgressPhase.finalize
    if "metadata" in name or "thumbnail" in name or "subtitle" in name or "chapter" in name:
        return ProgressPhase.postprocess
    return ProgressPhase.postprocess


def _postprocessor_message(postprocessor: str, status: str) -> str:
    verb = "finished" if status == "done" else "running" if status == "running" else status
    return f"{postprocessor} {verb}"


def _optional_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_size(value: str | None, unit: str | None) -> int | None:
    if not value or not unit:
        return None
    multiplier = _BYTE_UNITS.get(unit.upper())
    if multiplier is None:
        return None
    return int(float(value) * multiplier)


def _parse_eta(value: str | None) -> float | None:
    if not value:
        return None
    total = 0
    current = ""
    multipliers = {"h": 3600, "m": 60, "s": 1}
    for char in value:
        if char.isdigit():
            current += char
            continue
        if char in multipliers and current:
            total += int(current) * multipliers[char]
            current = ""
    if current and not total:
        return float(current)
    return float(total) if total else None
