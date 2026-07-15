"""Filesystem paths for atlas."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from platformdirs import PlatformDirs

APP_NAME = "atlas"
_DANGEROUS_FILENAME_CHARS = set('/\\:*?"<>|\0')
_MAX_FILENAME_LENGTH = 180


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
        directory.mkdir(parents=True, exist_ok=True)


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
    return _truncate_filename(cleaned, max_length=_MAX_FILENAME_LENGTH)


def _truncate_filename(name: str, *, max_length: int) -> str:
    if len(name) <= max_length:
        return name
    suffix = PurePosixPath(name).suffix
    if suffix and len(suffix) < max_length // 2:
        stem_limit = max_length - len(suffix)
        return f"{name[:stem_limit]}{suffix}"
    return name[:max_length]
