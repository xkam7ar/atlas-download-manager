"""Safe helpers for small Atlas files that may contain private URLs or headers."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def ensure_private_directory(path: Path) -> Path:
    """Create a real owner-only directory and reject symbolic-link destinations."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError(f"Refusing non-directory private path: {path}")
    path.chmod(0o700)
    return path


def write_private_text(path: Path, text: str) -> None:
    """Create a new owner-only text file without following symbolic links."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def replace_private_text(path: Path, text: str) -> None:
    """Atomically replace a small text file with owner-only contents."""

    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        write_private_text(temporary, text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
