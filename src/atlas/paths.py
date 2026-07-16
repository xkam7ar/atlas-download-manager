"""Filesystem paths for atlas."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from platformdirs import PlatformDirs

from atlas.private_files import ensure_private_directory

APP_NAME = "atlas"
_DANGEROUS_FILENAME_CHARS = set('/\\:*?"<>|\0')
_MAX_FILENAME_LENGTH = 180
_MAX_FILENAME_BYTES = 240
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def app_dirs() -> PlatformDirs:
    return PlatformDirs(APP_NAME, appauthor=False)


def config_dir() -> Path:
    return Path(app_dirs().user_config_dir)


def data_dir() -> Path:
    return Path(app_dirs().user_data_dir)


def cache_dir() -> Path:
    return Path(app_dirs().user_cache_dir)


def log_dir() -> Path:
    return Path(app_dirs().user_log_dir)


def config_path() -> Path:
    return config_dir() / "config.toml"


def archive_path() -> Path:
    return data_dir() / "download-archive.txt"


def default_output_dir() -> Path:
    return Path.home() / "Downloads" / APP_NAME


def ensure_app_dirs() -> None:
    for directory in (config_dir(), data_dir(), cache_dir(), log_dir()):
        ensure_private_directory(directory)


def safe_filename(value: str | None, *, default: str = "download") -> str:
    """Return a single safe filename segment while preserving useful Unicode."""

    if value is None:
        return default
    name = PurePosixPath(unquote(value.strip())).name
    if not name or name in {".", ".."}:
        return default
    cleaned = "".join(
        "_" if char in _DANGEROUS_FILENAME_CHARS or ord(char) < 32 or ord(char) == 127 else char
        for char in name
    ).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        return default
    if cleaned.partition(".")[0].upper() in _WINDOWS_RESERVED_STEMS:
        cleaned = f"_{cleaned}"
    return _truncate_filename(
        cleaned,
        max_length=_MAX_FILENAME_LENGTH,
        max_bytes=_MAX_FILENAME_BYTES,
    )


def _truncate_filename(name: str, *, max_length: int, max_bytes: int) -> str:
    if len(name) <= max_length and len(name.encode("utf-8")) <= max_bytes:
        return name
    suffix = PurePosixPath(name).suffix
    suffix_bytes = suffix.encode("utf-8")
    if suffix and len(suffix) < max_length // 2 and len(suffix_bytes) < max_bytes // 2:
        stem = name[: -len(suffix)][: max_length - len(suffix)]
        return f"{_utf8_prefix(stem, max_bytes - len(suffix_bytes))}{suffix}"
    return _utf8_prefix(name[:max_length], max_bytes)


def _utf8_prefix(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
