"""Stable parser facade for open-directory indexes."""

from __future__ import annotations

from atlas.directory_index import (
    DirectoryEntry,
    DirectoryEntryKind,
    DirectoryIndex,
    parse_directory_index,
)

__all__ = [
    "DirectoryEntry",
    "DirectoryEntryKind",
    "DirectoryIndex",
    "parse_directory_index",
]
