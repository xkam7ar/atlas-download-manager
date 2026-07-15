"""Stdlib logging setup."""

from __future__ import annotations

import logging as std_logging
import os
from io import TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

from atlas.paths import ensure_app_dirs, log_dir
from atlas.redaction import redact_text

_MAX_LOG_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating log handler that creates every generation with mode 0600."""

    def _open(self) -> TextIOWrapper:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.baseFilename, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        return cast(
            TextIOWrapper,
            open(
                descriptor,
                self.mode,
                encoding=self.encoding,
                errors=self.errors,
                closefd=True,
            ),
        )


class _RedactingFormatter(std_logging.Formatter):
    def format(self, record: std_logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(verbose: bool = False, log_file: Path | None = None) -> None:
    """Configure root logging once per command."""

    ensure_app_dirs()
    level = std_logging.DEBUG if verbose else std_logging.INFO
    stream_handler = std_logging.StreamHandler()
    stream_handler.setLevel(std_logging.INFO)
    handlers: list[std_logging.Handler] = [stream_handler]
    if log_file is None:
        log_file = log_dir() / "atlas.log"
    log_file = log_file.expanduser()
    log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_handler = _PrivateRotatingFileHandler(
        log_file,
        maxBytes=_MAX_LOG_BYTES,
        backupCount=_LOG_BACKUPS,
    )
    file_handler.setLevel(level)
    handlers.append(file_handler)
    formatter = _RedactingFormatter(_LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
    std_logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )
