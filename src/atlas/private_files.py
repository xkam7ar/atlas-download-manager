"""Safe helpers for small Atlas files that may contain private URLs or headers."""

from __future__ import annotations

import os
import stat
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
    completed = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        completed = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not completed:
            path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def prepare_private_file(path: Path) -> Path:
    """Create or harden a regular file without following a leaf symlink."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OSError(f"Refusing non-directory private parent: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"Refusing non-regular private file: {path}")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return path


def replace_private_text(path: Path, text: str) -> None:
    """Atomically replace a small text file with owner-only contents."""

    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        write_private_text(temporary, text)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def publish_private_file(source: Path, destination: Path) -> Path:
    """Atomically publish one regular file with owner-only permissions."""

    source_metadata = source.lstat()
    if not stat.S_ISREG(source_metadata.st_mode):
        raise OSError(f"Refusing non-regular private source: {source}")
    prepare_private_file(destination)
    source.chmod(0o600)
    os.replace(source, destination)
    destination.chmod(0o600)
    _fsync_directory(destination.parent)
    return destination


def _fsync_directory(path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
